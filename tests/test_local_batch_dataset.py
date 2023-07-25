import itertools
from typing import Sequence, Union

import jax
import jax.numpy as jnp
import numpy as np
from jax.experimental.maps import Mesh
from jaxtyping import PyTree
from test_utils import skip_if_not_enough_devices

import haliax
import levanter.data
from haliax import Axis
from haliax.partitioning import ResourceAxis
from levanter.data.sharded import LocalBatchDataset, check_sharded_consistency
from levanter.shapes import NamedShapeSpec, ShapeSpec


def _small_dataset(seq_len=128, num_sequences=200) -> levanter.data.ShardableDataset[Sequence[int]]:
    class SequenceDataset(levanter.data.ShardableDataset[np.ndarray]):
        def __init__(self, sequences: Sequence[np.ndarray]):
            self.sequences = sequences

        def __len__(self):
            return len(self.sequences)

        def shard(self, shard_idx: int, num_shards: int) -> levanter.data.ShardableDataset[np.ndarray]:
            return SequenceDataset(self.sequences[shard_idx::num_shards])

        def __iter__(self):
            yield from self.sequences

        @property
        def item_shape(self) -> PyTree[Union[ShapeSpec, NamedShapeSpec]]:
            return ShapeSpec((seq_len,), dtype=np.int32)

    # sequences = [list(range(i * 1000, i * 1000 + seq_len)) for i in range(num_sequences)]
    sequences = [np.arange(seq_len) + 1000 * i for i in range(num_sequences)]

    return SequenceDataset(sequences)


@skip_if_not_enough_devices(2)
def test_local_batched_data_loading_model_axis_2():
    devices = jax.devices()
    model_axis_size = 2

    mesh = Mesh(
        np.array(devices).reshape(-1, model_axis_size),
        (ResourceAxis.DATA, ResourceAxis.MODEL),
    )
    with mesh, haliax.axis_mapping({"batch": ResourceAxis.DATA}):

        seq_len = 128
        cache = _small_dataset(seq_len)
        Batch = Axis("batch", len(devices))
        dataset = LocalBatchDataset(cache, mesh, Batch)

        batches = list(itertools.islice(dataset, 10))
        for batch in batches:
            assert batch.shape == dataset.item_shape.shape
            check_sharded_consistency(batch, check_disjoint_indices_are_different=True)


def test_local_batched_data_loading_model_axis_1():
    devices = jax.devices()
    model_axis_size = 1

    mesh = Mesh(
        np.array(devices).reshape(-1, model_axis_size),
        (ResourceAxis.DATA, ResourceAxis.MODEL),
    )
    with mesh, haliax.axis_mapping({"batch": ResourceAxis.DATA}):

        seq_len = 128
        cache = _small_dataset(seq_len)
        Batch = Axis("batch", len(devices))
        dataset = LocalBatchDataset(cache, mesh, Batch)

        batches = list(itertools.islice(dataset, 10))
        for batch in batches:
            assert batch.shape == dataset.item_shape.shape
            check_sharded_consistency(batch, check_disjoint_indices_are_different=True)


class StructuredDataset(levanter.data.ShardableDataset):
    def __init__(self, seq_len, begin, end, stride):
        self.seq_len = seq_len
        self.begin = begin
        self.end = end
        self.stride = stride

    def __len__(self):
        return (self.end - self.begin) // self.stride

    def __getitem__(self, item):
        return {
            "input_ids": np.arange(self.seq_len, dtype=np.int32) + item * 1000,
            "labels": np.arange(self.seq_len, dtype=np.int32) + item * 1000,
            "extra": {
                "input_ids": np.arange(self.seq_len, dtype=np.int32) + item * 1000,
                "mask": np.arange(self.seq_len * 2, dtype=np.int32).reshape(-1, 2) + item * 1000,
            },
        }

    @property
    def item_shape(self) -> PyTree[Union[ShapeSpec, NamedShapeSpec]]:
        return {
            "input_ids": ShapeSpec((self.seq_len,), jnp.int32),
            "labels": ShapeSpec((self.seq_len,), jnp.int32),
            "extra": {
                "input_ids": ShapeSpec((self.seq_len,), jnp.int32),
                "mask": ShapeSpec((self.seq_len, 2), jnp.int32),
            },
        }

    def __iter__(self):
        for i in range(self.begin, self.end, self.stride):
            yield self[i]

    def shard(self, shard_id: int, num_shards: int):
        return StructuredDataset(self.seq_len, self.begin + shard_id, self.end, self.stride * num_shards)


def test_structured_batches_model_axis_1():
    devices = jax.devices()
    model_axis_size = 1

    mesh = Mesh(
        np.array(devices).reshape(-1, model_axis_size),
        (ResourceAxis.DATA, ResourceAxis.MODEL),
    )
    with mesh, haliax.axis_mapping({"batch": ResourceAxis.DATA}):
        seq_len = 128
        dataset = StructuredDataset(seq_len, 0, 256, 1)
        Batch = Axis("batch", len(devices))
        dataset = LocalBatchDataset(dataset, mesh, Batch)

        batches = list(itertools.islice(dataset, 10))
        for batch in batches:
            check_sharded_consistency(batch, check_disjoint_indices_are_different=True)


@skip_if_not_enough_devices(2)
def test_structured_batches_model_axis_2():
    devices = jax.devices()
    model_axis_size = 2

    mesh = Mesh(
        np.array(devices).reshape(-1, model_axis_size),
        (ResourceAxis.DATA, ResourceAxis.MODEL),
    )
    with mesh, haliax.axis_mapping({"batch": ResourceAxis.DATA}):
        seq_len = 128
        dataset = StructuredDataset(seq_len, 0, 256, 1)
        Batch = Axis("batch", len(devices))
        dataset = LocalBatchDataset(dataset, mesh, Batch)

        batches = list(itertools.islice(dataset, 10))
        for batch in batches:
            check_sharded_consistency(batch, check_disjoint_indices_are_different=True)


class StructuredDatasetWithNames(levanter.data.ShardableDataset):
    def __init__(self, Height: Axis, Width: Axis, begin, end, stride):
        self.Height = Height
        self.Width = Width
        self.begin = begin
        self.end = end
        self.stride = stride

    def __len__(self):
        return (self.end - self.begin) // self.stride

    def _gen_image(self, index):
        image = (
            np.arange(self.Height.size * self.Width.size, dtype=np.int32).reshape(self.Height.size, self.Width.size)
            + index * 1000
        )

        return haliax.named(image, (self.Height, self.Width))

    def __getitem__(self, item):
        return {
            "input_ids": self._gen_image(item),
            "labels": self._gen_image(item),
            "extra": {
                "input_ids": self._gen_image(item),
                "mask": haliax.arange(self.Height) + item * 1000,
            },
        }

    @property
    def item_shape(self) -> PyTree[Union[ShapeSpec, NamedShapeSpec]]:
        return {
            "input_ids": NamedShapeSpec((self.Height, self.Width), jnp.int32),
            "labels": NamedShapeSpec((self.Height, self.Width), jnp.int32),
            "extra": {
                "input_ids": NamedShapeSpec((self.Height, self.Width), jnp.int32),
                "mask": NamedShapeSpec((self.Height,), jnp.int32),
            },
        }

    def __iter__(self):
        for i in range(self.begin, self.end, self.stride):
            yield self[i]

    def shard(self, shard_id: int, num_shards: int):
        return StructuredDatasetWithNames(
            self.Height, self.Width, self.begin + shard_id, self.end, self.stride * num_shards
        )


def test_structured_batches_model_axis_1_with_names():
    devices = jax.devices()
    model_axis_size = 1

    mesh = Mesh(
        np.array(devices).reshape(-1, model_axis_size),
        (ResourceAxis.DATA, ResourceAxis.MODEL),
    )
    with mesh, haliax.axis_mapping({"batch": ResourceAxis.DATA}):
        Height = Axis("Height", 16)
        Width = Axis("Width", 16)
        dataset = StructuredDatasetWithNames(Height, Width, 0, 256, 1)
        Batch = Axis("batch", len(devices))
        dataset = LocalBatchDataset(dataset, mesh, Batch)

        batches = list(itertools.islice(dataset, 10))
        for batch in batches:
            check_sharded_consistency(batch, check_disjoint_indices_are_different=True)


@skip_if_not_enough_devices(2)
def test_structured_batches_model_axis_2_with_names():
    devices = jax.devices()
    model_axis_size = 2

    mesh = Mesh(
        np.array(devices).reshape(-1, model_axis_size),
        (ResourceAxis.DATA, ResourceAxis.MODEL),
    )
    with mesh, haliax.axis_mapping({"batch": ResourceAxis.DATA}):
        Height = Axis("Height", 16)
        Width = Axis("Width", 16)
        dataset = StructuredDatasetWithNames(Height, Width, 0, 256, 1)
        Batch = Axis("batch", len(devices))
        dataset = LocalBatchDataset(dataset, mesh, Batch)

        batches = list(itertools.islice(dataset, 10))
        for batch in batches:
            check_sharded_consistency(batch, check_disjoint_indices_are_different=True)


@skip_if_not_enough_devices(4)
def test_structured_batches_model_axis_2_subsharded():
    """This tests data loading if individual datums are sharded too"""
    devices = jax.devices()
    model_axis_size = 2

    mesh = Mesh(
        np.array(devices).reshape(-1, model_axis_size),
        (ResourceAxis.DATA, ResourceAxis.MODEL),
    )
    Height = Axis("Height", 16)
    Width = Axis("Width", 16)
    with mesh, haliax.axis_mapping({"batch": ResourceAxis.DATA, Height.name: ResourceAxis.MODEL}):
        dataset = StructuredDatasetWithNames(Height, Width, 0, 256, 1)
        Batch = Axis("batch", len(devices))
        dataset = LocalBatchDataset(dataset, mesh, Batch)

        batches = list(itertools.islice(dataset, 10))
        for batch in batches:
            check_sharded_consistency(batch, check_disjoint_indices_are_different=True)
