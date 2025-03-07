# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

from __future__ import annotations

import abc
import collections
import functools
import tempfile
import textwrap
import uuid
from collections import defaultdict
from collections.abc import Mapping
from copy import copy, deepcopy
from numbers import Number
from textwrap import indent
from typing import (
    Any,
    Callable,
    Dict,
    Generator,
    Iterator,
    List,
    Optional,
    OrderedDict,
    Sequence,
    Set,
    Tuple,
    Type,
    Union,
)
from warnings import warn

import numpy as np
import torch

from tensordict.utils import _get_item, _is_shared, _requires_grad, _set_item, _shape
from torch import Tensor
from torch.utils._pytree import tree_map

try:
    from torch.jit._shape_functions import infer_size_impl
except ImportError:
    from tensordict.utils import infer_size_impl

from tensordict.memmap import MemmapTensor
from tensordict.metatensor import MetaTensor
from tensordict.utils import (
    _getitem_batch_size,
    _nested_key_type_check,
    _sub_index,
    convert_ellipsis_to_idx,
    DEVICE_TYPING,
    expand_as_right,
    expand_right,
    INDEX_TYPING,
    KeyDependentDefaultDict,
    NESTED_KEY,
    prod,
)

# from torch.utils._pytree import _register_pytree_node


_has_functorch = False
try:
    try:
        from functorch._C import is_batchedtensor
    except ImportError:
        from torch._C._functorch import is_batchedtensor

    _has_functorch = True
except ImportError:
    _has_functorch = False

    def is_batchedtensor(tensor):
        """Placeholder for the functorch function."""
        return False


try:
    from torchrec import KeyedJaggedTensor

    _has_torchrec = True
except ImportError as err:
    _has_torchrec = False

    class KeyedJaggedTensor:
        pass

    TORCHREC_ERR = str(err)


TD_HANDLED_FUNCTIONS: Dict = {}
COMPATIBLE_TYPES = Union[
    Tensor,
    MemmapTensor,
]  # None? # leaves space for TensorDictBase

if _has_torchrec:
    COMPATIBLE_TYPES = Union[
        Tensor,
        MemmapTensor,
        KeyedJaggedTensor,
    ]
_STR_MIXED_INDEX_ERROR = "Received a mixed string-non string index. Only string-only or string-free indices are supported."


class _TensorDictKeysView:
    """
    _TensorDictKeysView is returned when accessing tensordict.keys() and holds a
    reference to the original TensorDict. This class enables us to support nested keys
    when performing membership checks and when iterating over keys.

    Examples:
        >>> import torch
        >>> from tensordict import TensorDict

        >>> td = TensorDict(
        >>>     {"a": TensorDict({"b": torch.rand(1, 2)}, [1, 2]), "c": torch.rand(1)},
        >>>     [1],
        >>> )

        >>> assert "a" in td.keys()
        >>> assert ("a",) in td.keys()
        >>> assert ("a", "b") in td.keys()
        >>> assert ("a", "c") not in td.keys()

        >>> assert set(td.keys()) == {("a", "b"), "c"}
    """

    def __init__(
        self, tensordict: "TensorDictBase", include_nested: bool, leaves_only: bool
    ):
        self.tensordict = tensordict
        self.include_nested = include_nested
        self.leaves_only = leaves_only
        self.loop_set = set()

    def __iter__(self):
        if not self.include_nested:
            if self.leaves_only:
                for key in self._keys():
                    meta_val = self.tensordict._get_meta(key)
                    if meta_val.is_tensordict():
                        continue
                    yield key
            else:
                yield from self._keys()
        else:
            yield from self._iter_helper(self.tensordict)

    def _iter_helper(self, tensordict, prefix=None):
        items_iter = self._items(tensordict)
        self.loop_set.add(id(tensordict))
        for key, value in items_iter:
            full_key = self._combine_keys(prefix, key)
            if (
                isinstance(value, (TensorDictBase, KeyedJaggedTensor))
                and self.include_nested
                and id(value) not in self.loop_set
            ):
                subkeys = tuple(
                    self._iter_helper(
                        value,
                        full_key if isinstance(full_key, tuple) else (full_key,),
                    )
                )
                yield from subkeys
            if not (isinstance(value, TensorDictBase) and self.leaves_only):
                yield full_key
        self.loop_set.remove(id(tensordict))

    def _combine_keys(self, prefix, key):
        if prefix is not None:
            if type(key) is tuple:
                return prefix + key
            return prefix + (key,)
        return key

    def __len__(self):
        i = 0
        for _ in self:
            i += 1
        return i

    def _items(self, tensordict=None):
        if tensordict is None:
            tensordict = self.tensordict
        if isinstance(tensordict, TensorDict):
            return tensordict._tensordict.items()
        elif isinstance(tensordict, LazyStackedTensorDict):
            return _iter_items_lazystack(tensordict)
        elif isinstance(tensordict, KeyedJaggedTensor):
            return tuple((key, tensordict[key]) for key in tensordict.keys())
        elif isinstance(tensordict, _CustomOpTensorDict):
            # it's possible that a TensorDict contains a nested LazyStackedTensorDict,
            # or _CustomOpTensorDict, so as we iterate through the contents we need to
            # be careful to not rely on tensordict._tensordict existing.
            return ((key, tensordict.get(key)) for key in tensordict._source.keys())

    def _keys(self):
        return self.tensordict._tensordict.keys()

    def __contains__(self, key):
        if type(key) is str:
            if key in self._keys():
                if self.leaves_only:
                    meta_val = self.tensordict._get_meta(key)
                    return not meta_val.is_tensordict()
                return True
            return False

        elif type(key) is tuple:
            if len(key) == 1:
                return key[0] in self
            elif len(key) > 1:
                if self.include_nested:
                    if key[0] in self:
                        meta_val = self.tensordict._get_meta(key[0])
                        # TODO: SavedTensorDict currently doesn't support nested membership checks
                        include_nested = self.include_nested  # and not isinstance(
                        #     val, SavedTensorDict
                        # )
                        _tensordict_nested = meta_val.is_tensordict() and key[
                            1:
                        ] in self.tensordict.get(key[0]).keys(
                            include_nested=include_nested
                        )
                        _kjt = (
                            meta_val.is_kjt()
                            and len(key) == 2
                            and key[1] in self.tensordict.get(key[0]).keys()
                        )
                        return _kjt or _tensordict_nested

                    return False
            if all(isinstance(subkey, str) for subkey in key):
                raise TypeError(
                    "Nested membership checks with tuples of strings is only supported "
                    "when setting `include_nested=True`."
                )

        raise TypeError(
            "TensorDict keys are always strings. Membership checks are only supported "
            "for strings or non-empty tuples of strings (for nested TensorDicts)"
        )


class TensorDictBase(Mapping, metaclass=abc.ABCMeta):
    """TensorDictBase is an abstract parent class for TensorDicts, a torch.Tensor data container."""

    _safe = False
    _lazy = False
    _inplace_set = False
    is_meta = False

    def __getstate__(self) -> Dict[str, Any]:
        state = self.__dict__.copy()
        del state["_dict_meta"]
        return state

    def __setstate__(self, state: Dict[str, Any]) -> Dict[str, Any]:
        state["_dict_meta"] = KeyDependentDefaultDict(self._make_meta)
        self.__dict__.update(state)

    def __init__(self):
        self._dict_meta = KeyDependentDefaultDict(self._make_meta)
        self._being_repr = False
        self._being_flattened = False

    @abc.abstractmethod
    def _make_meta(self, key: str) -> MetaTensor:
        raise NotImplementedError

    @property
    def shape(self) -> torch.Size:
        """See :obj:`TensorDictBase.batch_size`."""
        return self.batch_size

    @property
    @abc.abstractmethod
    def batch_size(self) -> torch.Size:
        """Shape of (or batch_size) of a TensorDict.

        The shape of a tensordict corresponds to the common N first
        dimensions of the tensors it contains, where N is an arbitrary
        number. The TensorDict shape is controlled by the user upon
        initialization (i.e. it is not inferred from the tensor shapes) and
        it should not be changed dynamically.

        Returns:
            a torch.Size object describing the TensorDict batch size.

        """
        raise NotImplementedError

    def size(self, dim: Optional[int] = None):
        """Returns the size of the dimension indicated by :obj:`dim`.

        If dim is not specified, returns the batch_size (or shape) of the TensorDict.

        """
        if dim is None:
            return self.batch_size
        return self.batch_size[dim]

    @property
    def requires_grad(self):
        return any(v.requires_grad for v in self.values())

    def _batch_size_setter(self, new_batch_size: torch.Size) -> None:
        if new_batch_size == self.batch_size:
            return
        if self._lazy:
            raise RuntimeError(
                "modifying the batch size of a lazy repesentation of a "
                "tensordict is not permitted. Consider instantiating the "
                "tensordict first by calling `td = td.to_tensordict()` before "
                "resetting the batch size."
            )
        if self.batch_size == new_batch_size:
            return
        if not isinstance(new_batch_size, torch.Size):
            new_batch_size = torch.Size(new_batch_size)
        for key, meta in self.items_meta():
            if meta.is_tensordict():
                tensordict = self.get(key)
                if len(tensordict.batch_size) < len(new_batch_size):
                    # document as edge case
                    tensordict.batch_size = new_batch_size
                    self.set(key, tensordict)
                    meta.shape = new_batch_size
        self._check_new_batch_size(new_batch_size)
        self._change_batch_size(new_batch_size)

    @property
    def batch_dims(self) -> int:
        """Length of the tensordict batch size.

        Returns:
            int describing the number of dimensions of the tensordict.

        """
        return len(self.batch_size)

    def ndimension(self) -> int:
        return self.batch_dims

    def dim(self) -> int:
        return self.batch_dims

    @property
    @abc.abstractmethod
    def device(self) -> Union[None, torch.device]:
        """Device of a TensorDict.

        If the TensorDict has a specified device, all
        tensors of a tensordict must live on the same device. If the TensorDict device
        is None, then different values can be located on different devices.

        Returns:
            torch.device object indicating the device where the tensors
            are placed, or None if TensorDict does not have a device.

        """
        raise NotImplementedError

    @device.setter
    @abc.abstractmethod
    def device(self, value: DEVICE_TYPING) -> None:
        raise NotImplementedError

    def clear_device(self) -> None:
        self._device = None

    def is_shared(self) -> bool:
        """Checks if tensordict is in shared memory.

        If a TensorDict instance is in shared memory, any new tensor written
        in it will be placed in shared memory. If a TensorDict is created with
        tensors that are all in shared memory, this does not mean that it will be
        in shared memory (as a new tensor may not be in shared memory).
        Only if one calls `tensordict.share_memory_()` or places the tensordict
        on a device where the content is shared will the tensordict be considered
        in shared memory.

        This is always True for CUDA tensordicts, except when stored as
        MemmapTensors.

        """
        if self.device and not self._is_memmap:
            return self.device.type == "cuda" or self._is_shared
        return self._is_shared

    def state_dict(self) -> OrderedDict:
        out = collections.OrderedDict()
        for key, item in self.flatten_keys().items():
            out[key] = item
        if "__batch_size" in out:
            raise KeyError(
                "Cannot retrieve the state_dict of a TensorDict with `'__batch_size'` key"
            )
        if "__device" in out:
            raise KeyError(
                "Cannot retrieve the state_dict of a TensorDict with `'__batch_size'` key"
            )
        out["__batch_size"] = self.batch_size
        out["__device"] = self.device
        return out

    def load_state_dict(self, state_dict: OrderedDict) -> TensorDictBase:
        self.batch_size = state_dict.pop("__batch_size")
        device = state_dict.pop("__device")
        if device is not None:
            self.to(device)
        self.update(state_dict, inplace=True)
        return self

    def is_memmap(self) -> bool:
        """Checks if tensordict is stored with MemmapTensors."""
        return self._is_memmap

    def numel(self) -> int:
        """Total number of elements in the batch."""
        return max(1, prod(self.batch_size))

    def _check_batch_size(self) -> None:
        bs = [value.shape[: self.batch_dims] for key, value in self.items_meta()] + [
            self.batch_size
        ]
        if len(set(bs)) > 1:
            raise RuntimeError(
                f"batch_size are incongruent, got {list(set(bs))}, "
                f"-- expected {self.batch_size}"
            )

    def _check_is_shared(self) -> bool:
        raise NotImplementedError(f"{self.__class__.__name__}")

    def _check_device(self) -> None:
        raise NotImplementedError(f"{self.__class__.__name__}")

    def set(
        self, key: NESTED_KEY, item: COMPATIBLE_TYPES, inplace: bool = False, **kwargs
    ) -> TensorDictBase:
        """Sets a new key-value pair.

        Args:
            key (str): name of the value
            item (torch.Tensor): value to be stored in the tensordict
            inplace (bool, optional): if True and if a key matches an existing
                key in the tensordict, then the update will occur in-place
                for that key-value pair. Default is :obj:`False`.

        Returns:
            self

        """
        raise NotImplementedError(f"{self.__class__.__name__}")

    @abc.abstractmethod
    def set_(
        self, key: NESTED_KEY, item: COMPATIBLE_TYPES, no_check: bool = False
    ) -> TensorDictBase:
        """Sets a value to an existing key while keeping the original storage.

        Args:
            key (str): name of the value
            item (torch.Tensor): value to be stored in the tensordict
            no_check (bool, optional): if True, it is assumed that device and shape
                match the original tensor and that the keys is in the tensordict.

        Returns:
            self

        """
        raise NotImplementedError(f"{self.__class__.__name__}")

    @abc.abstractmethod
    def _stack_onto_(
        self,
        key: str,
        list_item: List[COMPATIBLE_TYPES],
        dim: int,
    ) -> TensorDictBase:
        """Stacks a list of values onto an existing key while keeping the original storage.

        Args:
            key (str): name of the value
            list_item (list of torch.Tensor): value to be stacked and stored in the tensordict.
            dim (int): dimension along which the tensors should be stacked.

        Returns:
            self

        """
        raise NotImplementedError(f"{self.__class__.__name__}")

    def _stack_onto_at_(
        self,
        key: str,
        list_item: List[COMPATIBLE_TYPES],
        dim: int,
        idx: INDEX_TYPING,
    ) -> TensorDictBase:
        """Similar to _stack_onto_ but on a specific index. Only works with regular TensorDicts."""
        raise RuntimeError(
            f"Cannot call _stack_onto_at_ with {self.__class__.__name__}. "
            "This error is probably caused by a call to a lazy operation before stacking. "
            "Make sure your sub-classed tensordicts are turned into regular tensordicts by calling to_tensordict() "
            "before calling __getindex__ and stack."
        )

    def _default_get(
        self, key: str, default: Union[str, COMPATIBLE_TYPES] = "_no_default_"
    ) -> COMPATIBLE_TYPES:
        if not isinstance(default, str):
            return default
        if default == "_no_default_":
            raise KeyError(
                f'key "{key}" not found in {self.__class__.__name__} with '
                f"keys {sorted(self.keys())}"
            )
        else:
            raise ValueError(
                f"default should be None or a Tensor instance, " f"got {default}"
            )

    @abc.abstractmethod
    def get(
        self, key: NESTED_KEY, default: Union[str, COMPATIBLE_TYPES] = "_no_default_"
    ) -> COMPATIBLE_TYPES:
        """Gets the value stored with the input key.

        Args:
            key (str): key to be queried.
            default: default value if the key is not found in the tensordict.

        """
        raise NotImplementedError(f"{self.__class__.__name__}")

    def pop(
        self, key: NESTED_KEY, default: Union[str, COMPATIBLE_TYPES] = "_no_default_"
    ) -> COMPATIBLE_TYPES:
        _nested_key_type_check(key)
        try:
            # using try/except for get/del is suboptimal, but
            # this is faster that checkink if key in self keys
            out = self.get(key, default)
            self.del_(key)
        except KeyError:
            # if default provided, 'out' value will return, else raise error
            if default == "_no_default_":
                raise KeyError(
                    f"You are trying to pop key `{key}` which is not in dict"
                    f"without providing default value."
                )
        return out

    def _get_meta(self, key: NESTED_KEY) -> MetaTensor:
        # _nested_key_type_check(key)
        try:
            if type(key) is tuple:
                if len(key) > 1:
                    return self.get(key[0])._get_meta(key[1:])
                key = key[0]
            return self._dict_meta[key]
        except KeyError:
            # TODO: this error message will make more sense if `.keys()` returns nested
            # keys rather than just the top level ones
            raise KeyError(
                f"key {key} not found in {self.__class__.__name__} with keys"
                f" {sorted(self.keys(), key=str)}"
            )

    def apply_(self, fn: Callable) -> TensorDictBase:
        """Applies a callable to all values stored in the tensordict and re-writes them in-place.

        Args:
            fn (Callable): function to be applied to the tensors in the
                tensordict.

        Returns:
            self or a copy of self with the function applied

        """
        return self.apply(fn, inplace=True)

    def apply(
        self,
        fn: Callable,
        batch_size: Optional[Sequence[int]] = None,
        inplace: bool = False,
        **constructor_kwargs,
    ) -> TensorDictBase:
        """Applies a callable to all values stored in the tensordict and sets them in a new tensordict.

        Args:
            fn (Callable): function to be applied to the tensors in the
                tensordict.
            batch_size (sequence of int, optional): if provided,
                the resulting TensorDict will have the desired batch_size.
                The :obj:`batch_size` argument should match the batch_size after
                the transformation.
            inplace (bool, optional): if True, changes are made in-place.
                Default is False.
            **constructor_kwargs: additional keyword arguments to be passed to the
                TensorDict constructor.

        Returns:
            a new tensordict with transformed_in tensors.

        """
        out = (
            self
            if inplace
            else TensorDict(
                {},
                batch_size=batch_size,
                device=self.device,
                _run_checks=False,
                **constructor_kwargs,
            )
            if batch_size is not None
            else self.clone(recurse=False)
        )
        is_locked = out.is_locked
        if not inplace and is_locked:
            out.unlock()
        for key, item in self.items():
            if isinstance(item, TensorDictBase):
                item_trsf = item.apply(
                    fn, inplace=inplace, batch_size=batch_size, **constructor_kwargs
                )
            else:
                item_trsf = fn(item)
            if item_trsf is not None:
                out.set(
                    key, item_trsf, inplace=inplace, _run_checks=False, _process=False
                )
        if not inplace and is_locked:
            out.lock()
        return out

    def update(
        self,
        input_dict_or_td: Union[Dict[str, COMPATIBLE_TYPES], TensorDictBase],
        clone: bool = False,
        inplace: bool = False,
        **kwargs,
    ) -> TensorDictBase:
        """Updates the TensorDict with values from either a dictionary or another TensorDict.

        Args:
            input_dict_or_td (TensorDictBase or dict): Does not keyword arguments
                (unlike :obj:`dict.update()`).
            clone (bool, optional): whether the tensors in the input (
                tensor) dict should be cloned before being set. Default is
                `False`.
            inplace (bool, optional): if True and if a key matches an existing
                key in the tensordict, then the update will occur in-place
                for that key-value pair. Default is :obj:`False`.
            **kwargs: keyword arguments for the :obj:`TensorDict.set` method

        Returns:
            self

        """
        if input_dict_or_td is self:
            # no op
            return self
        keys = set(self.keys(False))
        for key, value in input_dict_or_td.items():
            if clone and hasattr(value, "clone"):
                value = value.clone()
            if type(key) is tuple:
                key, subkey = key[0], key[1:]
            else:
                subkey = []
            # the key must be a string by now. Let's check if it is present
            if key in keys:
                target = self._get_meta(key)
                if target.is_tensordict():
                    target = self.get(key)
                    if len(subkey):
                        target.update({subkey: value})
                        continue
                    elif isinstance(value, (dict, TensorDictBase)):
                        target.update(value)
                        continue
            self.set(key, value, inplace=inplace, **kwargs)
        return self

    def update_(
        self,
        input_dict_or_td: Union[Dict[str, COMPATIBLE_TYPES], TensorDictBase],
        clone: bool = False,
    ) -> TensorDictBase:
        """Updates the TensorDict in-place with values from either a dictionary or another TensorDict.

        Unlike TensorDict.update, this function will
        throw an error if the key is unknown to the TensorDict

        Args:
            input_dict_or_td (TensorDictBase or dict): Does not keyword
                arguments (unlike :obj:`dict.update()`).
            clone (bool, optional): whether the tensors in the input (
                tensor) dict should be cloned before being set. Default is
                `False`.

        Returns:
            self

        """
        if input_dict_or_td is self:
            # no op
            return self
        for key, value in input_dict_or_td.items():
            # if not isinstance(value, _accepted_classes):
            #     raise TypeError(
            #         f"Expected value to be one of types {_accepted_classes} "
            #         f"but got {type(value)}"
            #     )
            if clone:
                value = value.clone()
            self.set_(key, value)
        return self

    def update_at_(
        self,
        input_dict_or_td: Union[Dict[str, COMPATIBLE_TYPES], TensorDictBase],
        idx: INDEX_TYPING,
        clone: bool = False,
    ) -> TensorDictBase:
        """Updates the TensorDict in-place at the specified index with values from either a dictionary or another TensorDict.

        Unlike  TensorDict.update, this function will throw an error if the key is unknown to the TensorDict.

        Args:
            input_dict_or_td (TensorDictBase or dict): Does not keyword arguments
                (unlike :obj:`dict.update()`).
            idx (int, torch.Tensor, iterable, slice): index of the tensordict
                where the update should occur.
            clone (bool, optional): whether the tensors in the input (
                tensor) dict should be cloned before being set. Default is
                `False`.

        Returns:
            self

        Examples:
            >>> td = TensorDict(source={'a': torch.zeros(3, 4, 5),
            ...    'b': torch.zeros(3, 4, 10)}, batch_size=[3, 4])
            >>> td.update_at_(
            ...    TensorDict(source={'a': torch.ones(1, 4, 5),
            ...        'b': torch.ones(1, 4, 10)}, batch_size=[1, 4]),
            ...    slice(1, 2))
            TensorDict(
                fields={
                    a: Tensor(torch.Size([3, 4, 5]), dtype=torch.float32),
                    b: Tensor(torch.Size([3, 4, 10]), dtype=torch.float32)},
                batch_size=torch.Size([3, 4]),
                device=None,
                is_shared=False)

        """
        for key, value in input_dict_or_td.items():
            if not isinstance(value, _accepted_classes):
                raise TypeError(
                    f"Expected value to be one of types {_accepted_classes} "
                    f"but got {type(value)}"
                )
            if clone:
                value = value.clone()
            self.set_at_(
                key,
                value,
                idx,
            )
        return self

    def _convert_to_tensor(self, array: np.ndarray) -> Union[Tensor, MemmapTensor]:
        return torch.as_tensor(array, device=self.device)

    def _convert_to_tensordict(self, dict_value: Dict[str, Any]) -> TensorDictBase:
        return TensorDict(dict_value, batch_size=self.batch_size, device=self.device)

    def _process_input(
        self,
        input: Union[COMPATIBLE_TYPES, dict, np.ndarray],
        check_device: bool = True,
        check_tensor_shape: bool = True,
        check_shared: bool = False,
    ) -> Union[Tensor, MemmapTensor]:

        if isinstance(input, dict):
            tensor = self._convert_to_tensordict(input)
        elif not isinstance(input, _accepted_classes):
            tensor = self._convert_to_tensor(input)
        else:
            tensor = input

        if check_device and self.device is not None:
            device = self.device
            tensor = tensor.to(device)

        if check_tensor_shape and _shape(tensor)[: self.batch_dims] != self.batch_size:
            # if TensorDict, let's try to map it to the desired shape
            if (
                isinstance(tensor, TensorDictBase)
                and tensor.batch_size[: self.batch_dims] != self.batch_size
            ):
                tensor = tensor.clone(recurse=False)
                tensor.batch_size = self.batch_size
            else:
                raise RuntimeError(
                    f"batch dimension mismatch, got self.batch_size"
                    f"={self.batch_size} and tensor.shape[:self.batch_dims]"
                    f"={_shape(tensor)[: self.batch_dims]} with tensor {tensor}"
                )

        return tensor

    @abc.abstractmethod
    def pin_memory(self) -> TensorDictBase:
        """Calls :obj:`pin_memory` on the stored tensors."""
        raise NotImplementedError(f"{self.__class__.__name__}")

    def items(
        self, include_nested: bool = False, leaves_only: bool = False
    ) -> Iterator[Tuple[str, COMPATIBLE_TYPES]]:
        """Returns a generator of key-value pairs for the tensordict."""
        for k in self.keys(include_nested=include_nested, leaves_only=leaves_only):
            yield k, self.get(k)

    def values(
        self, include_nested: bool = False, leaves_only: bool = False
    ) -> Iterator[COMPATIBLE_TYPES]:
        """Returns a generator representing the values for the tensordict."""
        for k in self.keys(include_nested=include_nested, leaves_only=leaves_only):
            yield self.get(k)

    def items_meta(
        self,
        make_unset: bool = True,
        include_nested: bool = False,
        leaves_only: bool = False,
    ) -> Iterator[Tuple[str, MetaTensor]]:
        """Returns a generator of key-value pairs for the tensordict.

        The values are MetaTensor instances corresponding to the stored tensors.

        """
        if make_unset:
            for k in self.keys(include_nested=include_nested, leaves_only=leaves_only):
                yield k, self._get_meta(k)
        else:
            for k, item in self._dict_meta.items():
                if item.is_tensordict():
                    if not leaves_only:
                        yield k, item
                    if include_nested:
                        yield from self.get(k).items_meta(
                            make_unset=make_unset,
                            include_nested=include_nested,
                            leaves_only=leaves_only,
                        )
                else:
                    yield k, item

    def values_meta(
        self,
        make_unset: bool = True,
        include_nested: bool = False,
        leaves_only: bool = False,
    ) -> Iterator[MetaTensor]:
        """Returns a generator representing the values for the tensordict.

        Those values are MetaTensor instances corresponding to the stored tensors.

        """
        if make_unset:
            for k in self.keys(include_nested=include_nested, leaves_only=leaves_only):
                yield self._get_meta(k)
        else:
            for k, item in self._dict_meta.items():
                if item.is_tensordict():
                    if not leaves_only:
                        yield item
                    if include_nested:
                        yield from self.get(k).values_meta(
                            make_unset=make_unset,
                            include_nested=include_nested,
                            leaves_only=leaves_only,
                        )
                else:
                    yield item

    @abc.abstractmethod
    def keys(
        self, include_nested: bool = False, leaves_only: bool = False
    ) -> _TensorDictKeysView:
        """Returns a generator of tensordict keys."""
        raise NotImplementedError(f"{self.__class__.__name__}")

    def expand(self, *shape) -> TensorDictBase:
        """Expands each tensors of the tensordict according to the torch.expand function.

        In practice, this amends to: :obj:`tensor.expand(*shape, *tensor.shape)`.

        Supports iterables to specify the shape

        Examples:
            >>> td = TensorDict(source={'a': torch.zeros(3, 4, 5),
            ...     'b': torch.zeros(3, 4, 10)}, batch_size=[3, 4])
            >>> td_expand = td.expand(10, 3, 4)
            >>> assert td_expand.shape == torch.Size([10, 3, 4])
            >>> assert td_expand.get("a").shape == torch.Size([10, 3, 4, 5])

        """
        d = {}
        tensordict_dims = self.batch_dims

        if len(shape) == 1 and isinstance(shape[0], Sequence):
            shape = tuple(shape[0])

        # new shape dim check
        if len(shape) < len(self.shape):
            raise RuntimeError(
                "the number of sizes provided ({shape_dim}) must be greater or equal to the number of "
                "dimensions in the TensorDict ({tensordict_dim})".format(
                    shape_dim=len(shape), tensordict_dim=tensordict_dims
                )
            )

        # new shape compatability check
        for old_dim, new_dim in zip(self.batch_size, shape[-tensordict_dims:]):
            if old_dim != 1 and new_dim != old_dim:
                raise RuntimeError(
                    "Incompatible expanded shape: The expanded shape length at non-singleton dimension should be same "
                    "as the original length. target_shape = {new_shape}, existing_shape = {old_shape}".format(
                        new_shape=shape, old_shape=self.batch_size
                    )
                )
        for key, value in self.items():
            tensor_dims = len(value.shape)
            last_n_dims = tensor_dims - tensordict_dims
            if last_n_dims > 0:
                d[key] = value.expand(*shape, *value.shape[-last_n_dims:])
            else:
                d[key] = value.expand(*shape)
        return TensorDict(
            source=d,
            batch_size=[*shape],
            device=self.device,
            _run_checks=False,
        )

    def __bool__(self) -> bool:
        raise ValueError("Converting a tensordict to boolean value is not permitted")

    def __ne__(self, other: object) -> TensorDictBase:
        """XOR operation over two tensordicts, for evey key.

        The two tensordicts must have the same key set.

        Args:
            other (TensorDictBase, dict, or float): the value to compare against.

        Returns:
            a new TensorDict instance with all tensors are boolean
            tensors of the same shape as the original tensors.

        """
        if not isinstance(other, (TensorDictBase, dict, float, int)):
            return False
        if not isinstance(other, TensorDictBase) and isinstance(other, dict):
            other = make_tensordict(**other, batch_size=self.batch_size)
        if not isinstance(other, TensorDictBase):
            return TensorDict(
                {key: value != other for key, value in self.items()},
                self.batch_size,
                device=self.device,
            )
        keys1 = set(self.keys())
        keys2 = set(other.keys())
        if len(keys1.difference(keys2)) or len(keys1) != len(keys2):
            raise KeyError(
                f"keys in {self} and {other} mismatch, got {keys1} and {keys2}"
            )
        d = {}
        for (key, item1) in self.items():
            d[key] = item1 != other.get(key)
        return TensorDict(batch_size=self.batch_size, source=d, device=self.device)

    def __eq__(self, other: object) -> TensorDictBase:
        """Compares two tensordicts against each other, for every key. The two tensordicts must have the same key set.

        Returns:
            a new TensorDict instance with all tensors are boolean
            tensors of the same shape as the original tensors.

        """

        def __eq__(
            tensordict, other, current_key: Tuple = None, being_computed: Dict = None
        ):
            """A version of __eq__ that supports auto-nesting."""
            if not isinstance(other, (TensorDictBase, dict, float, int)):
                return False
            if not isinstance(other, TensorDictBase) and isinstance(other, dict):
                other = make_tensordict(**other, batch_size=tensordict.batch_size)
            if not isinstance(other, TensorDictBase):
                return TensorDict(
                    {key: value == other for key, value in tensordict.items()},
                    tensordict.batch_size,
                    device=tensordict.device,
                )
            keys1 = set(tensordict.keys())
            keys2 = set(other.keys())
            if len(keys1.difference(keys2)) or len(keys1) != len(keys2):
                raise KeyError(f"keys in tensordicts mismatch, got {keys1} and {keys2}")
            out_dict = {}

            out_dict = {}
            if current_key is None:
                current_key = ()
            if being_computed is None:
                being_computed = {}
            being_computed[current_key] = id(tensordict)
            for key, value in tensordict.items():
                if isinstance(value, TensorDictBase):
                    nested_key = current_key + (key,)
                    if id(value) in being_computed.values():
                        being_computed[nested_key] = id(value)
                        continue
                    new_value = __eq__(
                        value,
                        other[key],
                        current_key=nested_key,
                        being_computed=being_computed,
                    )
                else:
                    new_value = value == other[key]
                out_dict[key] = new_value

            out = TensorDict(
                out_dict,
                device=self.device,
                batch_size=self.batch_size,
            )
            for other_nested_key, other_value in being_computed.items():
                if other_nested_key != current_key:
                    if other_value == id(tensordict):
                        out[other_nested_key] = out
            return out

        return __eq__(tensordict=self, other=other)

    @abc.abstractmethod
    def del_(self, key: str) -> TensorDictBase:
        """Deletes a key of the tensordict.

        Args:
            key (str): key to be deleted

        Returns:
            self

        """
        raise NotImplementedError(f"{self.__class__.__name__}")

    @abc.abstractmethod
    def select(
        self, *keys: str, inplace: bool = False, strict: bool = True
    ) -> TensorDictBase:
        """Selects the keys of the tensordict and returns an new tensordict with only the selected keys.

        The values are not copied: in-place modifications a tensor of either
        of the original or new tensordict will result in a change in both
        tensordicts.

        Args:
            *keys (str): keys to select
            inplace (bool): if True, the tensordict is pruned in place.
                Default is :obj:`False`.
            strict (bool, optional): whether selecting a key that is not present
                will return an error or not. Default: :obj:`True`.

        Returns:
            A new tensordict with the selected keys only.

        """
        raise NotImplementedError(f"{self.__class__.__name__}")

    def exclude(self, *keys: str, inplace: bool = False) -> TensorDictBase:
        target = self if inplace else self.clone(recurse=False)
        # is_nested = any((type(key) is tuple) for key in keys)
        # if len(keys) > 1:
        #     tdkeys = set(self.keys(is_nested))
        # else:
        #     tdkeys = self.keys(is_nested)
        for key in keys:
            try:
                del target[key]
            except KeyError:
                continue
        return target

    @abc.abstractmethod
    def set_at_(
        self, key: NESTED_KEY, value: COMPATIBLE_TYPES, idx: INDEX_TYPING
    ) -> TensorDictBase:
        """Sets the values in-place at the index indicated by :obj:`idx`.

        Args:
            key (str): key to be modified.
            value (torch.Tensor): value to be set at the index `idx`
            idx (int, tensor or tuple): index where to write the values.

        Returns:
            self

        """
        raise NotImplementedError(f"{self.__class__.__name__}")

    def copy_(self, tensordict: TensorDictBase) -> TensorDictBase:
        """See :obj:`TensorDictBase.update_`."""
        return self.update_(tensordict)

    def copy_at_(self, tensordict: TensorDictBase, idx: INDEX_TYPING) -> TensorDictBase:
        """See :obj:`TensorDictBase.update_at_`."""
        return self.update_at_(tensordict, idx)

    def get_at(
        self, key: str, idx: INDEX_TYPING, default: COMPATIBLE_TYPES = "_no_default_"
    ) -> COMPATIBLE_TYPES:
        """Get the value of a tensordict from the key `key` at the index `idx`.

        Args:
            key (str): key to be retrieved.
            idx (int, slice, torch.Tensor, iterable): index of the tensor.
            default (torch.Tensor): default value to return if the key is
                not present in the tensordict.

        Returns:
            indexed tensor.

        """
        value = self.get(key, default=default)
        if value is not default:
            return value[idx]
        return value

    @abc.abstractmethod
    def share_memory_(self) -> TensorDictBase:
        """Places all the tensors in shared memory.

        The TensorDict is then locked, meaning that the only writing operations that
        can be executed must be done in-place.
        Once the tensordict is unlocked, the share_memory attribute is turned to False,
        because cross-process identity is not guaranteed anymore.

        Returns:
            self.

        """
        raise NotImplementedError(f"{self.__class__.__name__}")

    @abc.abstractmethod
    def memmap_(
        self,
        prefix=None,
    ) -> TensorDictBase:
        """Writes all tensors onto a MemmapTensor.

        Args:
            prefix (str): directory prefix where the memmap tensors will have to
                be stored.

        The TensorDict is then locked, meaning that the only writing operations that
        can be executed must be done in-place.
        Once the tensordict is unlocked, the memmap attribute is turned to False,
        because cross-process identity is not guaranteed anymore.

        Returns:
            self.

        """
        raise NotImplementedError(f"{self.__class__.__name__}")

    @abc.abstractmethod
    def detach_(self) -> TensorDictBase:
        """Detach the tensors in the tensordict in-place.

        Returns:
            self.

        """
        raise NotImplementedError(f"{self.__class__.__name__}")

    def detach(self) -> TensorDictBase:
        """Detach the tensors in the tensordict.

        Returns:
            a new tensordict with no tensor requiring gradient.

        """
        return TensorDict(
            {key: item.detach() for key, item in self.items()},
            batch_size=self.batch_size,
            device=self.device,
            _run_checks=False,
        )

    def to_tensordict(self):
        """Returns a regular TensorDict instance from the TensorDictBase.

        Returns:
            a new TensorDict object containing the same values.

        """

        def to_tensordict(
            tensordict, current_key: Tuple = None, being_computed: Dict = None
        ):
            """A version of to_tensordict that supports auto-nesting."""
            out_dict = {}
            if current_key is None:
                current_key = ()
            if being_computed is None:
                being_computed = {}
            being_computed[current_key] = id(tensordict)
            for key, value in tensordict.items():
                if isinstance(value, TensorDictBase):
                    nested_key = current_key + (key,)
                    if id(value) in being_computed.values():
                        being_computed[nested_key] = id(value)
                        continue
                    new_value = to_tensordict(
                        value, current_key=nested_key, being_computed=being_computed
                    )
                else:
                    new_value = value.clone()
                out_dict[key] = new_value
            out = TensorDict(
                out_dict,
                device=self.device,
                batch_size=self.batch_size,
                _run_checks=False,
            )
            for other_nested_key, other_value in being_computed.items():
                if other_nested_key != current_key:
                    if other_value == id(tensordict):
                        out[other_nested_key] = out
            return out

        return to_tensordict(self)

    def zero_(self) -> TensorDictBase:
        """Zeros all tensors in the tensordict in-place."""
        for key in self.keys():
            self.fill_(key, 0)
        return self

    def unbind(self, dim: int) -> Tuple[TensorDictBase, ...]:
        """Returns a tuple of indexed tensordicts unbound along the indicated dimension.

        Resulting tensordicts will share the storage of the initial tensordict.

        """
        idx = [
            (tuple(slice(None) for _ in range(dim)) + (i,))
            for i in range(self.shape[dim])
        ]
        if dim < 0:
            dim = self.batch_dims + dim
        batch_size = torch.Size([s for i, s in enumerate(self.batch_size) if i != dim])
        return tuple(
            self.apply(
                lambda tensor, idx=_idx: tensor[idx],
                batch_size=batch_size,
                _is_shared=self.is_shared(),
                _is_memmap=self.is_memmap(),
            )
            for _idx in idx
        )

    def chunk(self, chunks: int, dim: int = 0) -> Tuple[TensorDictBase, ...]:
        """Splits a tendordict into the specified number of chunks, if possible.

        Each chunk is a view of the input tensordict.

        Args:
            chunks (int): number of chunks to return
            dim (int, optional): dimension along which to split the
                tensordict. Default is 0.

        """
        if chunks < 1:
            raise ValueError(
                f"chunks must be a strictly positive integer, got {chunks}."
            )
        indices = []
        _idx_start = 0
        if chunks > 1:
            interval = _idx_end = self.batch_size[dim] // chunks
        else:
            interval = _idx_end = self.batch_size[dim]
        for c in range(chunks):
            indices.append(slice(_idx_start, _idx_end))
            _idx_start = _idx_end
            if c < chunks - 2:
                _idx_end = _idx_end + interval
            else:
                _idx_end = self.batch_size[dim]
        if dim < 0:
            dim = len(self.batch_size) + dim
        return tuple(self[(*[slice(None) for _ in range(dim)], idx)] for idx in indices)

    def clone(self, recurse: bool = True) -> TensorDictBase:
        """Clones a TensorDictBase subclass instance onto a new TensorDict.

        Args:
            recurse (bool, optional): if True, each tensor contained in the
                TensorDict will be copied too. Default is `True`.

        """

        def clone(tensordict, current_key: Tuple = None, being_computed: Dict = None):
            """A version of to_tensordict that supports auto-nesting."""
            out_dict = {}
            if current_key is None:
                current_key = ()
            if being_computed is None:
                being_computed = {}
            being_computed[current_key] = id(tensordict)
            for key, value in tensordict.items():
                if isinstance(value, TensorDictBase):
                    nested_key = current_key + (key,)
                    if id(value) in being_computed.values():
                        being_computed[nested_key] = id(value)
                        continue
                    new_value = clone(
                        value, current_key=nested_key, being_computed=being_computed
                    )
                else:
                    if not recurse:
                        new_value = value
                    else:
                        new_value = value.clone()
                out_dict[key] = new_value
            out = TensorDict(
                out_dict,
                device=tensordict.device,
                batch_size=tensordict.batch_size,
                _run_checks=False,
                _is_shared=tensordict.is_shared() if not recurse else False,
                _is_memmap=tensordict.is_memmap() if not recurse else False,
            )
            for other_nested_key, other_value in being_computed.items():
                if other_nested_key != current_key:
                    if other_value == id(tensordict):
                        out[other_nested_key] = out
            return out

        return clone(self)

    @classmethod
    def __torch_function__(
        cls,
        func: Callable,
        types,
        args: Tuple = (),
        kwargs: Optional[dict] = None,
    ) -> Callable:
        if kwargs is None:
            kwargs = {}
        if func not in TD_HANDLED_FUNCTIONS or not all(
            issubclass(t, (Tensor, TensorDictBase)) for t in types
        ):
            return NotImplemented
        return TD_HANDLED_FUNCTIONS[func](*args, **kwargs)

    @abc.abstractmethod
    def to(
        self, dest: Union[DEVICE_TYPING, Type, torch.Size], **kwargs
    ) -> TensorDictBase:
        """Maps a TensorDictBase subclass either on a new device or to another TensorDictBase subclass (if permitted).

        Casting tensors to a new dtype is not allowed, as tensordicts are not bound to contain a single
        tensor dtype.

        Args:
            dest (device, size or TensorDictBase subclass): destination of the
                tensordict. If it is a torch.Size object, the batch_size
                will be updated provided that it is compatible with the
                stored tensors.

        Returns:
            a new tensordict. If device indicated by dest differs from
            the tensordict device, this is a no-op.

        """
        raise NotImplementedError

    def _check_new_batch_size(self, new_size: torch.Size):
        n = len(new_size)
        for key, meta_tensor in self.items_meta():
            if meta_tensor.shape[:n] != new_size:
                raise RuntimeError(
                    f"the tensor {key} has shape {meta_tensor.shape} which "
                    f"is incompatible with the new shape {new_size}."
                )

    @abc.abstractmethod
    def _change_batch_size(self, new_size: torch.Size):
        raise NotImplementedError

    def cpu(self) -> TensorDictBase:
        """Casts a tensordict to CPU."""
        return self.to("cpu")

    def cuda(self, device: int = 0) -> TensorDictBase:
        """Casts a tensordict to a cuda device (if not already on it)."""
        return self.to(f"cuda:{device}")

    @abc.abstractmethod
    def masked_fill_(self, mask: Tensor, value: Union[float, bool]) -> TensorDictBase:
        """Fills the values corresponding to the mask with the desired value.

        Args:
            mask (boolean torch.Tensor): mask of values to be filled. Shape
                must match tensordict shape.
            value: value to used to fill the tensors.

        Returns:
            self

        Examples:
            >>> td = TensorDict(source={'a': torch.zeros(3, 4)},
            ...     batch_size=[3])
            >>> mask = torch.tensor([True, False, False])
            >>> _ = td.masked_fill_(mask, 1.0)
            >>> td.get("a")
            tensor([[1., 1., 1., 1.],
                    [0., 0., 0., 0.],
                    [0., 0., 0., 0.]])
        """
        raise NotImplementedError

    @abc.abstractmethod
    def masked_fill(self, mask: Tensor, value: Union[float, bool]) -> TensorDictBase:
        """Out-of-place version of masked_fill.

        Args:
            mask (boolean torch.Tensor): mask of values to be filled. Shape
                must match tensordict shape.
            value: value to used to fill the tensors.

        Returns:
            self

        Examples:
            >>> td = TensorDict(source={'a': torch.zeros(3, 4)},
            ...     batch_size=[3])
            >>> mask = torch.tensor([True, False, False])
            >>> td1 = td.masked_fill(mask, 1.0)
            >>> td1.get("a")
            tensor([[1., 1., 1., 1.],
                    [0., 0., 0., 0.],
                    [0., 0., 0., 0.]])
        """
        raise NotImplementedError

    def masked_select(self, mask: Tensor) -> TensorDictBase:
        """Masks all tensors of the TensorDict and return a new TensorDict instance with similar keys pointing to masked values.

        Args:
            mask (torch.Tensor): boolean mask to be used for the tensors.
                Shape must match the TensorDict batch_size.

        Examples:
            >>> td = TensorDict(source={'a': torch.zeros(3, 4)},
            ...    batch_size=[3])
            >>> mask = torch.tensor([True, False, False])
            >>> td_mask = td.masked_select(mask)
            >>> td_mask.get("a")
            tensor([[0., 0., 0., 0.]])

        """
        d = {}
        for key, value in self.items():
            while mask.ndimension() > self.batch_dims:
                mask_expand = mask.squeeze(-1)
            else:
                mask_expand = mask
            value_select = value[mask_expand]
            d[key] = value_select
        dim = int(mask.sum().item())
        return TensorDict(device=self.device, source=d, batch_size=torch.Size([dim]))

    @abc.abstractmethod
    def is_contiguous(self) -> bool:
        """Returns a boolean indicating if all the tensors are contiguous."""
        raise NotImplementedError

    @abc.abstractmethod
    def contiguous(self) -> TensorDictBase:
        """Returns a new tensordict of the same type with contiguous values (or self if values are already contiguous)."""
        raise NotImplementedError

    def to_dict(self) -> Dict[str, Any]:
        """Returns a dictionary with key-value pairs matching those of the tensordict."""
        return {
            key: value.to_dict() if isinstance(value, TensorDictBase) else value
            for key, value in self.items()
        }

    def unsqueeze(self, dim: int) -> TensorDictBase:
        """Unsqueeze all tensors for a dimension comprised in between `-td.batch_dims` and `td.batch_dims` and returns them in a new tensordict.

        Args:
            dim (int): dimension along which to unsqueeze

        """
        if dim < 0:
            dim = self.batch_dims + dim + 1

        if (dim > self.batch_dims) or (dim < 0):
            raise RuntimeError(
                f"unsqueezing is allowed for dims comprised between "
                f"`-td.batch_dims` and `td.batch_dims` only. Got "
                f"dim={dim} with a batch size of {self.batch_size}."
            )
        return _UnsqueezedTensorDict(
            source=self,
            custom_op="unsqueeze",
            inv_op="squeeze",
            custom_op_kwargs={"dim": dim},
            inv_op_kwargs={"dim": dim},
        )

    def squeeze(self, dim: Optional[int] = None) -> TensorDictBase:
        """Squeezes all tensors for a dimension comprised in between `-td.batch_dims+1` and `td.batch_dims-1` and returns them in a new tensordict.

        Args:
            dim (Optional[int]): dimension along which to squeeze. If dim is None, all singleton dimensions will be squeezed. dim is None by default.

        """
        if dim is None:
            size = self.size()
            if len(self.size()) == 1 or size.count(1) == 0:
                return self
            first_singleton_dim = size.index(1)

            squeezed_dict = _SqueezedTensorDict(
                source=self,
                custom_op="squeeze",
                inv_op="unsqueeze",
                custom_op_kwargs={"dim": first_singleton_dim},
                inv_op_kwargs={"dim": first_singleton_dim},
            )
            return squeezed_dict.squeeze(dim=None)

        if dim < 0:
            dim = self.batch_dims + dim

        if self.batch_dims and (dim >= self.batch_dims or dim < 0):
            raise RuntimeError(
                f"squeezing is allowed for dims comprised between 0 and "
                f"td.batch_dims only. Got dim={dim} and batch_size"
                f"={self.batch_size}."
            )

        if dim >= self.batch_dims or self.batch_size[dim] != 1:
            return self
        return _SqueezedTensorDict(
            source=self,
            custom_op="squeeze",
            inv_op="unsqueeze",
            custom_op_kwargs={"dim": dim},
            inv_op_kwargs={"dim": dim},
        )

    def reshape(
        self,
        *shape: int,
        size: Optional[Union[List, Tuple, torch.Size]] = None,
    ) -> TensorDictBase:
        """Returns a contiguous, reshaped tensor of the desired shape.

        Args:
            *shape (int): new shape of the resulting tensordict.
            size: iterable

        Returns:
            A TensorDict with reshaped keys

        """
        if len(shape) == 0 and size is not None:
            return self.view(*size)
        elif len(shape) == 1 and isinstance(shape[0], (list, tuple, torch.Size)):
            return self.view(*shape[0])
        elif not isinstance(shape, torch.Size):
            shape = torch.Size(shape)

        d = {}
        for key, item in self.items():
            d[key] = item.reshape(*shape, *item.shape[self.ndimension() :])
        if d:
            batch_size = d[key].shape[: len(shape)]
        else:
            if any(not isinstance(i, int) or i < 0 for i in shape):
                raise RuntimeError(
                    "Implicit reshaping is not permitted with empty " "tensordicts"
                )
            batch_size = shape
        return TensorDict(d, batch_size, device=self.device, _run_checks=False)

    def split(
        self, split_size: Union[int, List[int]], dim: int = 0
    ) -> List[TensorDictBase]:
        """Splits each tensor in the TensorDict with the specified size in the given dimension, like `torch.split`.
        Returns a list of TensorDict with the view of split chunks of items. Nested TensorDicts will remain nested.

        The list of TensorDict maintains the original order of the tensor chunks.

        Args:
            split_size (int or List(int)): size of a single chunk or list of sizes for each chunk
            dim (int): dimension along which to split the tensor

        Returns:
            A list of TensorDict with specified size in given dimension.

        """
        batch_sizes = []
        if self.batch_dims == 0:
            raise RuntimeError("TensorDict with empty batch size is not splittable")
        if not (-self.batch_dims <= dim < self.batch_dims):
            raise IndexError(
                f"Dimension out of range (expected to be in range of [-{self.batch_dims}, {self.batch_dims - 1}], but got {dim})"
            )
        if dim < 0:
            dim += self.batch_dims
        if isinstance(split_size, int):
            rep, remainder = divmod(self.batch_size[dim], split_size)
            rep_shape = [
                split_size if idx == dim else size
                for (idx, size) in enumerate(self.batch_size)
            ]
            batch_sizes = [rep_shape for _ in range(rep)]
            if remainder:
                batch_sizes.append(
                    [
                        remainder if dim_idx == dim else dim_size
                        for (dim_idx, dim_size) in enumerate(self.batch_size)
                    ]
                )
        elif isinstance(split_size, list) and all(
            isinstance(element, int) for element in split_size
        ):
            if sum(split_size) != self.batch_size[dim]:
                raise RuntimeError(
                    f"Split method expects split_size to sum exactly to {self.batch_size[dim]} (tensor's size at dimension {dim}), but got split_size={split_size}"
                )
            for i in split_size:
                batch_sizes.append(
                    [
                        i if dim_idx == dim else dim_size
                        for (dim_idx, dim_size) in enumerate(self.batch_size)
                    ]
                )
        else:
            raise TypeError(
                "split(): argument 'split_size' must be int or list of ints"
            )
        dictionaries = [{} for _ in range(len(batch_sizes))]
        for key, item in self.items():
            split_tensors = torch.split(item, split_size, dim)
            for idx, split_tensor in enumerate(split_tensors):
                dictionaries[idx][key] = split_tensor
        return [
            TensorDict(
                dictionaries[i],
                batch_sizes[i],
                device=self.device,
                _run_checks=False,
                _is_shared=self.is_shared(),
                _is_memmap=self.is_memmap(),
            )
            for i in range(len(dictionaries))
        ]

    def view(
        self,
        *shape: int,
        size: Optional[Union[List, Tuple, torch.Size]] = None,
    ) -> TensorDictBase:
        """Returns a tensordict with views of the tensors according to a new shape, compatible with the tensordict batch_size.

        Args:
            *shape (int): new shape of the resulting tensordict.
            size: iterable

        Returns:
            a new tensordict with the desired batch_size.

        Examples:
            >>> td = TensorDict(source={'a': torch.zeros(3,4,5),
            ...    'b': torch.zeros(3,4,10,1)}, batch_size=torch.Size([3, 4]))
            >>> td_view = td.view(12)
            >>> print(td_view.get("a").shape)  # torch.Size([12, 5])
            >>> print(td_view.get("b").shape)  # torch.Size([12, 10, 1])
            >>> td_view = td.view(-1, 4, 3)
            >>> print(td_view.get("a").shape)  # torch.Size([1, 4, 3, 5])
            >>> print(td_view.get("b").shape)  # torch.Size([1, 4, 3, 10, 1])

        """
        if len(shape) == 0 and size is not None:
            return self.view(*size)
        elif len(shape) == 1 and isinstance(shape[0], (list, tuple, torch.Size)):
            return self.view(*shape[0])
        elif not isinstance(shape, torch.Size):
            shape = infer_size_impl(shape, self.numel())
            shape = torch.Size(shape)
        if shape == self.shape:
            return self
        return _ViewedTensorDict(
            source=self,
            custom_op="view",
            inv_op="view",
            custom_op_kwargs={"size": shape},
            inv_op_kwargs={"size": self.batch_size},
        )

    def permute(
        self,
        *dims_list: int,
        dims=None,
    ) -> TensorDictBase:
        """Returns a view of a tensordict with the batch dimensions permuted according to dims.

        Args:
            *dims_list (int): the new ordering of the batch dims of the tensordict. Alternatively,
                a single iterable of integers can be provided.
            dims (list of int): alternative way of calling permute(...).

        Returns:
            a new tensordict with the batch dimensions in the desired order.

        Examples:
            >>> tensordict = TensorDict({"a": torch.randn(3, 4, 5)}, [3, 4])
            >>> print(tensordict.permute([1, 0]))
            PermutedTensorDict(
                source=TensorDict(
                    fields={
                        a: Tensor(torch.Size([3, 4, 5]), dtype=torch.float32)},
                    batch_size=torch.Size([3, 4]),
                    device=cpu,
                    is_shared=False),
                op=permute(dims=[1, 0]))
            >>> print(tensordict.permute(1, 0))
            PermutedTensorDict(
                source=TensorDict(
                    fields={
                        a: Tensor(torch.Size([3, 4, 5]), dtype=torch.float32)},
                    batch_size=torch.Size([3, 4]),
                    device=cpu,
                    is_shared=False),
                op=permute(dims=[1, 0]))
            >>> print(tensordict.permute(dims=[1, 0]))
            PermutedTensorDict(
                source=TensorDict(
                    fields={
                        a: Tensor(torch.Size([3, 4, 5]), dtype=torch.float32)},
                    batch_size=torch.Size([3, 4]),
                    device=cpu,
                    is_shared=False),
                op=permute(dims=[1, 0]))
        """
        if len(dims_list) == 0:
            dims_list = dims
        elif len(dims_list) == 1 and not isinstance(dims_list[0], int):
            dims_list = dims_list[0]
        if len(dims_list) != len(self.shape):
            raise RuntimeError(
                f"number of dims don't match in permute (got {len(dims_list)}, expected {len(self.shape)}"
            )

        if not len(dims_list) and not self.batch_dims:
            return self
        if np.array_equal(dims_list, range(self.batch_dims)):
            return self
        min_dim, max_dim = -self.batch_dims, self.batch_dims - 1
        seen = [False for dim in range(max_dim + 1)]
        for idx in dims_list:
            if idx < min_dim or idx > max_dim:
                raise IndexError(
                    f"dimension out of range (expected to be in range of [{min_dim}, {max_dim}], but got {idx})"
                )
            if seen[idx]:
                raise RuntimeError("repeated dim in permute")
            seen[idx] = True

        return _PermutedTensorDict(
            source=self,
            custom_op="permute",
            inv_op="permute",
            custom_op_kwargs={"dims": dims_list},
            inv_op_kwargs={"dims": dims_list},
        )

    def __repr__(self) -> str:
        if self._being_repr:
            return "Auto-nested"
        try:
            self._being_repr = True
            fields = _td_fields(self)
        finally:
            self._being_repr = False
        field_str = indent(f"fields={{{fields}}}", 4 * " ")
        batch_size_str = indent(f"batch_size={self.batch_size}", 4 * " ")
        device_str = indent(f"device={self.device}", 4 * " ")
        is_shared_str = indent(f"is_shared={self.is_shared()}", 4 * " ")
        string = ",\n".join([field_str, batch_size_str, device_str, is_shared_str])
        return f"{type(self).__name__}(\n{string})"

    def all(self, dim: int = None) -> Union[bool, TensorDictBase]:
        """Checks if all values are True/non-null in the tensordict.

        Args:
            dim (int, optional): if None, returns a boolean indicating
                whether all tensors return `tensor.all() == True`
                If integer, all is called upon the dimension specified if
                and only if this dimension is compatible with the tensordict
                shape.

        """
        if dim is not None and (dim >= self.batch_dims or dim <= -self.batch_dims):
            raise RuntimeError(
                "dim must be greater than -tensordict.batch_dims and smaller "
                "than tensordict.batchdims"
            )
        elif dim is not None and dim < 0:
            dim = self.batch_dims + dim

        def _all(tensordict, current_key: Tuple = None, being_computed: Dict = None):
            """A version of to_tensordict that supports auto-nesting."""
            out_dict = {}
            if current_key is None:
                current_key = ()
            if being_computed is None:
                being_computed = {}
            being_computed[current_key] = id(tensordict)
            for key, value in tensordict.items():
                if isinstance(value, TensorDictBase):
                    nested_key = current_key + (key,)
                    if id(value) in being_computed.values():
                        being_computed[nested_key] = id(value)
                        continue
                    new_value = _all(
                        value, current_key=nested_key, being_computed=being_computed
                    )
                else:
                    new_value = value.all(dim=dim) if dim is not None else value.all()
                out_dict[key] = new_value
            if dim is None:
                # no need to do anything with being_computed, as a True in it will be kept
                out = all(value for value in out_dict.values())
            else:
                out = TensorDict(
                    out_dict,
                    batch_size=[
                        b for i, b in enumerate(tensordict.batch_size) if i != dim
                    ],
                    device=tensordict.device,
                )
                for other_nested_key, other_value in being_computed.items():
                    if other_nested_key != current_key:
                        if other_value == id(tensordict):
                            out[other_nested_key] = out
            return out

        return _all(self)

    def any(self, dim: int = None) -> Union[bool, TensorDictBase]:
        """Checks if any value is True/non-null in the tensordict.

        Args:
            dim (int, optional): if None, returns a boolean indicating
                whether all tensors return `tensor.any() == True`.
                If integer, all is called upon the dimension specified if
                and only if this dimension is compatible with
                the tensordict shape.

        """
        if dim is not None and (dim >= self.batch_dims or dim <= -self.batch_dims):
            raise RuntimeError(
                "dim must be greater than -tensordict.batch_dims and smaller "
                "than tensordict.batchdims"
            )
        if dim is not None:
            if dim < 0:
                dim = self.batch_dims + dim
            return TensorDict(
                source={key: value.any(dim=dim) for key, value in self.items()},
                batch_size=[b for i, b in enumerate(self.batch_size) if i != dim],
                device=self.device,
            )
        return any([value.any() for key, value in self.items()])

    def get_sub_tensordict(self, idx: INDEX_TYPING) -> TensorDictBase:
        """Returns a SubTensorDict with the desired index."""
        return SubTensorDict(source=self, idx=idx)

    def __iter__(self) -> Generator:
        if not self.batch_dims:
            raise StopIteration
        length = self.batch_size[0]
        for i in range(length):
            yield self[i]

    def flatten_keys(
        self, separator: str = ".", inplace: bool = False
    ) -> TensorDictBase:
        if self._being_flattened:
            return self
        try:
            self._being_flattened = True
            to_flatten = []
            existing_keys = self.keys(include_nested=True)
            for key, meta_value in self.items_meta():
                key_split = tuple(key.split(separator))
                if meta_value.is_tensordict():
                    to_flatten.append(key)
                elif (
                    separator in key
                    and key_split in existing_keys
                    and not self._get_meta(key_split).is_tensordict()
                ):
                    raise KeyError(
                        f"Flattening keys in tensordict collides with existing key '{key}'"
                    )

            if inplace:
                for key in to_flatten:
                    inner_tensordict = self.get(key).flatten_keys(
                        separator=separator, inplace=inplace
                    )
                    for inner_key, inner_item in inner_tensordict.items():
                        self.set(separator.join([key, inner_key]), inner_item)
                for key in to_flatten:
                    del self[key]
                self._being_flattened = False
                return self
            else:
                tensordict_out = TensorDict(
                    {},
                    batch_size=self.batch_size,
                    device=self.device,
                    _run_checks=False,
                    _is_shared=self.is_shared(),
                    _is_memmap=self.is_memmap(),
                )
                for key, value in self.items():
                    if key in to_flatten:
                        inner_tensordict = self.get(key).flatten_keys(
                            separator=separator, inplace=inplace
                        )
                        if inner_tensordict is not self.get(key):
                            for inner_key, inner_item in inner_tensordict.items():
                                tensordict_out.set(
                                    separator.join([key, inner_key]), inner_item
                                )
                    else:
                        tensordict_out.set(key, value)
                self._being_flattened = False
                return tensordict_out
        finally:
            self._being_flattened = False

    def unflatten_keys(
        self, separator: str = ".", inplace: bool = False
    ) -> TensorDictBase:
        to_unflatten = defaultdict(list)
        for key in self.keys():
            if separator in key[1:-1]:
                split_key = key.split(separator)
                to_unflatten[split_key[0]].append((key, separator.join(split_key[1:])))

        if not inplace:
            out = TensorDict(
                {
                    key: value
                    for key, value in self.items()
                    if separator not in key[1:-1]
                },
                batch_size=self.batch_size,
                device=self.device,
                _run_checks=False,
                _is_shared=self.is_shared(),
                _is_memmap=self.is_memmap(),
            )
        else:
            out = self

        keys = set(out.keys())
        for key, list_of_keys in to_unflatten.items():

            if key in keys:
                raise KeyError(
                    "Unflattening key(s) in tensordict will override existing unflattened key"
                )

            tensordict = TensorDict({}, batch_size=self.batch_size, device=self.device)
            if key in self.keys():
                tensordict.update(self[key])
            for (old_key, new_key) in list_of_keys:
                value = self[old_key]
                tensordict[new_key] = value
                if inplace:
                    del self[old_key]
            out.set(key, tensordict.unflatten_keys(separator=separator))
        return out

    def __len__(self) -> int:
        """Returns the length of first dimension, if there is, otherwise 0."""
        return self.shape[0] if self.batch_dims else 0

    def __contains__(self, key):
        # by default a Mapping will implement __contains__ by calling __getitem__ and
        # returning False if a KeyError is raised, True otherwise. TensorDict has a
        # complex __getitem__ method since we support more than just retrieval of values
        # by key, and so this can be quite inefficient, particularly if values are
        # evaluated lazily on access. Hence we don't support use of __contains__ and
        # direct the user to use TensorDict.keys() instead
        raise NotImplementedError(
            "TensorDict does not support membership checks with the `in` keyword. If "
            "you want to check if a particular key is in your TensorDict, please use "
            "`key in tensordict.keys()` instead."
        )

    def _index_tensordict(self, idx: INDEX_TYPING):
        return TensorDict(
            source={key: _get_item(item, idx) for key, item in self.items()},
            _meta_source={
                key: _get_item(item, idx)
                for key, item in self.items_meta(make_unset=False)
                if not item.is_tensordict()
            },
            batch_size=_getitem_batch_size(self.batch_size, idx),
            device=self.device,
            _run_checks=False,
            _is_shared=self.is_shared(),
            _is_memmap=self.is_memmap(),
        )

    def __getitem__(self, idx: INDEX_TYPING) -> TensorDictBase:
        """Indexes all tensors according to the provided index.

        Returns a new tensordict where the values share the storage of the original tensors (even
        when the index is a torch.Tensor). Any in-place modification to the
        resulting tensordict will impact the parent tensordict too.

        Examples:
            >>> td = TensorDict(source={'a': torch.zeros(3,4,5)},
            ...     batch_size=torch.Size([3, 4]))
            >>> subtd = td[torch.zeros(1, dtype=torch.long)]
            >>> assert subtd.shape == torch.Size([1,4])
            >>> subtd.set("a", torch.ones(1,4,5))
            >>> print(td.get("a"))  # first row is full of 1
            >>> # Warning: this will not work as expected
            >>> subtd.get("a")[:] = 2.0
            >>> print(td.get("a"))  # values have not changed

        """
        if isinstance(idx, str) or (
            isinstance(idx, tuple) and all(isinstance(sub_idx, str) for sub_idx in idx)
        ):
            return self.get(idx)

        if not self.batch_size:
            raise RuntimeError(
                "indexing a tensordict with td.batch_dims==0 is not permitted"
            )

        if isinstance(idx, Number):
            return self._index_tensordict((idx,))

        if isinstance(idx, list):
            idx = torch.tensor(idx, device=self.device)
            return self._index_tensordict(idx)

        if isinstance(idx, np.ndarray):
            idx = torch.tensor(idx, device=self.device)
            return self._index_tensordict(idx)

        if isinstance(idx, tuple) and any(
            isinstance(sub_index, list) for sub_index in idx
        ):
            idx = tuple(
                torch.tensor(sub_index, device=self.device)
                if isinstance(sub_index, list)
                else sub_index
                for sub_index in idx
            )

        if isinstance(idx, tuple) and sum(
            isinstance(_idx, str) for _idx in idx
        ) not in [len(idx), 0]:
            raise IndexError(_STR_MIXED_INDEX_ERROR)

        if idx is Ellipsis or (isinstance(idx, tuple) and Ellipsis in idx):
            idx = convert_ellipsis_to_idx(idx, self.batch_size)

        # if return_simple_view and not self.is_memmap():
        return self._index_tensordict(idx)

    def __setitem__(
        self, index: INDEX_TYPING, value: Union[TensorDictBase, dict]
    ) -> None:
        if index is Ellipsis or (isinstance(index, tuple) and Ellipsis in index):
            index = convert_ellipsis_to_idx(index, self.batch_size)
        if isinstance(index, list):
            index = torch.tensor(index, device=self.device)
        if isinstance(index, tuple) and any(
            isinstance(sub_index, list) for sub_index in index
        ):
            index = tuple(
                torch.tensor(sub_index, device=self.device)
                if isinstance(sub_index, list)
                else sub_index
                for sub_index in index
            )
        if isinstance(index, tuple) and sum(
            isinstance(_index, str) for _index in index
        ) not in [len(index), 0]:
            raise IndexError(_STR_MIXED_INDEX_ERROR)
        if isinstance(index, str):
            self.set(index, value, inplace=self._inplace_set)
        elif isinstance(index, tuple) and isinstance(index[0], str):
            # TODO: would be nicer to have set handle the nested set, but the logic to
            # preserve the error handling below is complex and requires some thought
            try:
                if len(index) == 1:
                    return self.set(
                        index[0], value, inplace=isinstance(self, SubTensorDict)
                    )
                self.set(index, value, inplace=isinstance(self, SubTensorDict))
            except AttributeError as err:
                if "for populating tensordict with new key-value pair" in str(err):
                    raise RuntimeError(
                        "Trying to replace an existing nested tensordict with "
                        "another one with non-matching keys. This leads to "
                        "unspecified behaviours and is prohibited."
                    )
                raise err
        else:
            indexed_bs = _getitem_batch_size(self.batch_size, index)
            if isinstance(value, dict):
                value = TensorDict(
                    value, batch_size=indexed_bs, device=self.device, _run_checks=False
                )
            if value.batch_size != indexed_bs:
                raise RuntimeError(
                    f"indexed destination TensorDict batch size is {indexed_bs} "
                    f"(batch_size = {self.batch_size}, index={index}), "
                    f"which differs from the source batch size {value.batch_size}"
                )
            keys = set(self.keys())
            if not all(key in keys for key in value.keys()):
                subtd = self.get_sub_tensordict(index)
            for key, item in value.items():
                if key in keys:
                    self.set_at_(key, item, index)
                else:
                    subtd.set(key, item)

    def __delitem__(self, index: INDEX_TYPING) -> TensorDictBase:
        # if isinstance(index, str):
        return self.del_(index)
        # raise IndexError(f"Index has to a string but received {index}.")

    @abc.abstractmethod
    def rename_key(
        self, old_key: str, new_key: str, safe: bool = False
    ) -> TensorDictBase:
        """Renames a key with a new string.

        Args:
            old_key (str): key to be renamed
            new_key (str): new name
            safe (bool, optional): if True, an error is thrown when the new
                key is already present in the TensorDict.

        Returns:
            self

        """
        raise NotImplementedError

    def fill_(self, key: str, value: Union[float, bool]) -> TensorDictBase:
        """Fills a tensor pointed by the key with the a given value.

        Args:
            key (str): key to be remaned
            value (Number, bool): value to use for the filling

        Returns:
            self

        """
        meta_tensor = self._get_meta(key)
        shape = meta_tensor.shape
        device = meta_tensor.device
        dtype = meta_tensor.dtype
        if meta_tensor.is_tensordict():
            tensordict = self.get(key)
            tensordict.apply_(lambda x: x.fill_(value))
            self.set_(key, tensordict)
        else:
            tensor = torch.full(shape, value, device=device, dtype=dtype)
            self.set_(key, tensor)
        return self

    def empty(self) -> TensorDictBase:
        """Returns a new, empty tensordict with the same device and batch size."""
        return self.select()

    def is_empty(self):
        for _ in self.items_meta():
            return False
        return True

    @property
    def is_locked(self):
        if not hasattr(self, "_is_locked"):
            self._is_locked = False
        return self._is_locked

    @is_locked.setter
    def is_locked(self, value: bool):
        if value:
            self.lock()
        else:
            self.unlock()

    def set_default(
        self, key: NESTED_KEY, default: COMPATIBLE_TYPES, **kwargs
    ) -> COMPATIBLE_TYPES:
        """Insert key with a value of default if key is not in the dictionary.

        Return the value for key if key is in the dictionary, else default.

        Args:
            key (str): the name of the value.
            default (torch.Tensor): value to be stored in the tensordict if the key is
                not already present.

        Returns:
            The value of key in the tensordict. Will be default if the key was not
            previously set.

        """
        if key not in self.keys(include_nested=(type(key) is tuple)):
            self.set(key, default, **kwargs)
        return self.get(key)

    def lock(self):
        self._is_locked = True
        for key, item in self.items_meta():
            if item.is_tensordict():
                self.get(key).lock()

    def unlock(self):
        self._is_locked = False
        self._is_shared = False
        self._is_memmap = False
        for key, item in self.items_meta():
            if item.is_tensordict():
                self.get(key).unlock()


class TensorDict(TensorDictBase):
    """A batched dictionary of tensors.

    TensorDict is a tensor container where all tensors are stored in a
    key-value pair fashion and where each element shares at least the
    following features:
    - memory location (shared, memory-mapped array, ...);
    - batch size (i.e. n^th first dimensions).

    Additionally, if the tensordict has a specified device, then each element
    must share that device.

    TensorDict instances support many regular tensor operations as long as
    they are dtype-independent (as a TensorDict instance can contain tensors
    of many different dtypes). Those operations include (but are not limited
    to):

    - operations on shape: when a shape operation is called (indexing,
      reshape, view, expand, transpose, permute,
      unsqueeze, squeeze, masking etc), the operations is done as if it
      was done on a tensor of the same shape as the batch size then
      expended to the right, e.g.:

        >>> td = TensorDict({'a': torch.zeros(3,4,5)}, batch_size=[3, 4])
        >>> # returns a TensorDict of batch size [3, 4, 1]
        >>> td_unsqueeze = td.unsqueeze(-1)
        >>> # returns a TensorDict of batch size [12]
        >>> td_view = td.view(-1)
        >>> # returns a tensor of batch size [12, 4]
        >>> a_view = td.view(-1).get("a")

    - casting operations: a TensorDict can be cast on a different device
      or another TensorDict type using

        >>> td_cpu = td.to("cpu")
        >>> td_savec = td.to(SavedTensorDict)  # TensorDict saved on disk
        >>> dictionary = td.to_dict()

      A call of the `.to()` method with a dtype will return an error.

    - Cloning, contiguous

    - Reading: `td.get(key)`, `td.get_at(key, index)`

    - Content modification: :obj:`td.set(key, value)`, :obj:`td.set_(key, value)`,
      :obj:`td.update(td_or_dict)`, :obj:`td.update_(td_or_dict)`, :obj:`td.fill_(key,
      value)`, :obj:`td.rename_key(old_name, new_name)`, etc.

    - Operations on multiple tensordicts: `torch.cat(tensordict_list, dim)`,
      `torch.stack(tensordict_list, dim)`, `td1 == td2` etc.

    Args:
        source (TensorDict or dictionary): a data source. If empty, the
            tensordict can be populated subsequently.
        batch_size (iterable of int, optional): a batch size for the
            tensordict. The batch size is immutable and can only be modified
            by calling operations that create a new TensorDict. Unless the
            source is another TensorDict, the batch_size argument must be
            provided as it won't be inferred from the data.
        device (torch.device or compatible type, optional): a device for the
            TensorDict.

    Examples:
        >>> import torch
        >>> from tensordict import TensorDict
        >>> source = {'random': torch.randn(3, 4),
        ...     'zeros': torch.zeros(3, 4, 5)}
        >>> batch_size = [3]
        >>> td = TensorDict(source, batch_size)
        >>> print(td.shape)  # equivalent to td.batch_size
        torch.Size([3])
        >>> td_unqueeze = td.unsqueeze(-1)
        >>> print(td_unqueeze.get("zeros").shape)
        torch.Size([3, 1, 4, 5])
        >>> print(td_unqueeze[0].shape)
        torch.Size([1])
        >>> print(td_unqueeze.view(-1).shape)
        torch.Size([3])
        >>> print((td.clone()==td).all())
        True

    """

    @classmethod
    def __new__(cls, *args, **kwargs):
        cls._safe = True
        cls._lazy = False
        cls._is_shared = False
        cls._is_memmap = False
        return TensorDictBase.__new__(cls)

    def __init__(
        self,
        source: Union[TensorDictBase, dict],
        batch_size: Optional[Union[Sequence[int], torch.Size, int]] = None,
        device: Optional[DEVICE_TYPING] = None,
        _meta_source: Optional[dict] = None,
        _run_checks: bool = True,
        _is_shared: Optional[bool] = False,
        _is_memmap: Optional[bool] = False,
    ) -> None:
        super().__init__()

        self._is_shared = _is_shared
        self._is_memmap = _is_memmap
        if device is not None:
            device = torch.device(device)
        self._device = device

        if not _run_checks:
            if isinstance(source, dict):
                self._tensordict: Dict = copy(source)
            else:
                self._tensordict: Dict = dict(source)
            self._batch_size = torch.Size(batch_size)
            upd_dict = {}
            for key, value in self._tensordict.items():
                if isinstance(value, dict):
                    value = TensorDict(
                        value,
                        batch_size=self._batch_size,
                        device=self._device,
                        _run_checks=_run_checks,
                        _is_shared=_is_shared,
                        _is_memmap=_is_memmap,
                    )
                    upd_dict[key] = value
            if upd_dict:
                self._tensordict.update(upd_dict)
        else:
            self._tensordict = {}
            if not isinstance(source, (TensorDictBase, dict)):
                raise ValueError(
                    "A TensorDict source is expected to be a TensorDictBase "
                    f"sub-type or a dictionary, found type(source)={type(source)}."
                )
            self._batch_size = self._parse_batch_size(source, batch_size)

            if source is not None:
                for key, value in source.items():
                    if isinstance(value, dict):
                        value = TensorDict(
                            value,
                            batch_size=self._batch_size,
                            device=self._device,
                            _run_checks=_run_checks,
                            _is_shared=_is_shared,
                            _is_memmap=_is_memmap,
                        )
                    elif (
                        isinstance(value, TensorDictBase)
                        and value.batch_size[: self.batch_dims] != self.batch_size
                    ):
                        value = value.clone(False)
                        value.batch_size = self.batch_size
                    elif isinstance(value, (Tensor, MemmapTensor)):
                        if value.shape[: len(self._batch_size)] != self._batch_size:
                            raise RuntimeError(
                                f"batch_size are incongruent, got {value.shape}, -- expected leading dims to be {self._batch_size}"
                            )
                    if device is not None:
                        value = value.to(device)
                    _meta_val = (
                        None
                        if _meta_source is None or key not in _meta_source
                        else _meta_source[key]
                    )
                    self.set(key, value, _meta_val=_meta_val, _run_checks=False)

            # self._check_batch_size()
            # self._check_device()

    @staticmethod
    def _parse_batch_size(
        source: Union[TensorDictBase, dict],
        batch_size: Optional[Union[Sequence[int], torch.Size, int]] = None,
    ):
        if isinstance(batch_size, (Number, Sequence)):
            if not isinstance(batch_size, torch.Size):
                if isinstance(batch_size, int):
                    return torch.Size([batch_size])
                return torch.Size(batch_size)
            return batch_size
        elif isinstance(source, TensorDictBase):
            return source.batch_size
        raise ValueError(
            "batch size was not specified when creating the TensorDict "
            "instance and it could not be retrieved from source."
        )

    def _make_meta(self, key: str) -> MetaTensor:
        proc_value = self._tensordict[key]
        is_memmap = (
            self._is_memmap
            if self._is_memmap is not None
            else isinstance(proc_value, MemmapTensor)
        )
        is_shared = (
            self._is_shared
            if self._is_shared is not None
            else _is_shared(proc_value)
            if isinstance(proc_value, (TensorDictBase, MemmapTensor, KeyedJaggedTensor))
            or not is_batchedtensor(proc_value)
            else False
        )

        return MetaTensor(
            proc_value,
            device=proc_value.device,
            _is_memmap=is_memmap,
            _is_shared=is_shared,
            _is_tensordict=isinstance(proc_value, TensorDictBase),
        )

    @property
    def batch_dims(self) -> int:
        return len(self.batch_size)

    @batch_dims.setter
    def batch_dims(self, value: COMPATIBLE_TYPES) -> None:
        raise RuntimeError(
            f"Setting batch dims on {self.__class__.__name__} instances is "
            f"not allowed."
        )

    @property
    def device(self) -> Union[None, torch.device]:
        """Device of the tensordict.

        Returns `None` if device hasn't been provided in the constructor or set via `tensordict.to(device)`.

        """
        return self._device

    @device.setter
    def device(self, value: DEVICE_TYPING) -> None:
        raise RuntimeError(
            "device cannot be set using tensordict.device = device, "
            "because device cannot be updated in-place. To update device, use "
            "tensordict.to(new_device), which will return a new tensordict "
            "on the new device."
        )

    @property
    def batch_size(self) -> torch.Size:
        return self._batch_size

    @batch_size.setter
    def batch_size(self, new_size: torch.Size):
        return self._batch_size_setter(new_size)

    def _change_batch_size(self, new_size: torch.Size):
        if not hasattr(self, "_orig_batch_size"):
            self._orig_batch_size = self.batch_size
        elif self._orig_batch_size == new_size:
            del self._orig_batch_size
        self._batch_size = new_size

    # Checks
    def _check_is_shared(self) -> bool:
        share_list = [_is_shared(value) for key, value in self.items_meta()]
        if any(share_list) and not all(share_list):
            shared_str = ", ".join(
                [f"{key}: {_is_shared(value)}" for key, value in self.items_meta()]
            )
            raise RuntimeError(
                f"tensors must be either all shared or not, but mixed "
                f"features is not allowed. "
                f"Found: {shared_str}"
            )
        return all(share_list) and len(share_list) > 0

    def _check_is_memmap(self) -> bool:
        memmap_list = [value.is_memmap() for key, value in self.items_meta()]
        if any(memmap_list) and not all(memmap_list):
            memmap_str = ", ".join(
                [f"{key}: {value.is_memmap()}" for key, value in self.items_meta()]
            )
            raise RuntimeError(
                f"tensors must be either all MemmapTensor or not, but mixed "
                f"features is not allowed. "
                f"Found: {memmap_str}"
            )
        return all(memmap_list) and len(memmap_list) > 0

    def _check_device(self) -> None:
        devices = {value.device for value in self.values_meta()}
        if self.device is not None and len(devices) >= 1 and devices != {self.device}:
            raise RuntimeError(
                f"TensorDict.device is {self._device}, but elements have "
                f"device values {devices}. If TensorDict.device is set then "
                "all elements must share that device."
            )

    def _index_tensordict(self, idx: INDEX_TYPING):
        self_copy = copy(self)
        self_copy._tensordict = {
            key: _get_item(item, idx) for key, item in self.items()
        }
        self_copy._dict_meta = KeyDependentDefaultDict(self_copy._make_meta)
        self_copy._batch_size = _getitem_batch_size(self_copy.batch_size, idx)
        self_copy._device = self.device
        return self_copy

    def pin_memory(self) -> TensorDictBase:
        if self.device and self.device.type == "cpu":
            for key, value in self.items():
                if isinstance(value, TensorDictBase) or (
                    value.dtype in (torch.half, torch.float, torch.double)
                ):
                    self.set(key, value.pin_memory(), inplace=False)
        return self

    def expand(self, *shape) -> TensorDictBase:
        """Expands every tensor with `(*shape, *tensor.shape)` and returns the same tensordict with new tensors with expanded shapes.

        Supports iterables to specify the shape.

        """
        d = {}
        tensordict_dims = self.batch_dims

        if len(shape) == 1 and isinstance(shape[0], Sequence):
            shape = tuple(shape[0])

        # new shape dim check
        if len(shape) < len(self.shape):
            raise RuntimeError(
                "the number of sizes provided ({shape_dim}) must be greater or equal to the number of "
                "dimensions in the TensorDict ({tensordict_dim})".format(
                    shape_dim=len(shape), tensordict_dim=tensordict_dims
                )
            )

        # new shape compatability check
        for old_dim, new_dim in zip(self.batch_size, shape[-tensordict_dims:]):
            if old_dim != 1 and new_dim != old_dim:
                raise RuntimeError(
                    "Incompatible expanded shape: The expanded shape length at non-singleton dimension should be same "
                    "as the original length. target_shape = {new_shape}, existing_shape = {old_shape}".format(
                        new_shape=shape, old_shape=self.batch_size
                    )
                )

        for key, value in self.items():
            tensor_dims = len(value.shape)
            last_n_dims = tensor_dims - tensordict_dims
            if last_n_dims > 0:
                d[key] = value.expand(*shape, *value.shape[-last_n_dims:])
            else:
                d[key] = value.expand(*shape)
        return TensorDict(
            source=d,
            batch_size=[*shape],
            device=self.device,
            _run_checks=False,
        )

    def set(
        self,
        key: NESTED_KEY,
        value: Union[dict, COMPATIBLE_TYPES],
        inplace: bool = False,
        _run_checks: bool = True,
        _meta_val: Optional[MetaTensor] = None,
        _process: bool = True,
    ) -> TensorDictBase:
        """Sets a value in the TensorDict.

        If inplace=True (default is False), and if the key already exists, set will call set_ (in place setting).

        """
        if self.is_locked:
            if not inplace or key not in self.keys():
                raise RuntimeError(
                    "Cannot modify locked TensorDict. For in-place modification, consider using the `set_()` method."
                )
        # if _run_checks:
        #     _nested_key_type_check(key)

        if type(key) is tuple and len(key) == 1:
            key = key[0]

        if value is self._tensordict.get(key, None):
            return self

        if inplace and key in self.keys(True):
            return self.set_(key, value)

        if type(key) is str:
            if _process:
                proc_value = self._process_input(
                    value,
                    check_tensor_shape=_run_checks,
                    check_shared=False,
                    check_device=_run_checks,
                )
            else:
                proc_value = value

            self._tensordict[key] = proc_value
            if _meta_val:
                self._dict_meta[key] = _meta_val
            else:
                self._dict_meta.pop(key, None)
        else:
            # since we call _nested_key_type_check above, we may assume that the key is
            # a tuple of strings
            td, subkey = _get_leaf_tensordict(self, key, _default_hook)
            td.set(
                subkey,
                value,
                inplace=inplace,
                _process=_process,
                _run_checks=_run_checks,
            )

            if _meta_val:
                td._dict_meta[subkey] = _meta_val
            elif subkey in td._dict_meta:
                del td._dict_meta[subkey]

        return self

    def del_(self, key: str) -> TensorDictBase:
        if type(key) is tuple:
            td, subkey = _get_leaf_tensordict(self, key)
            del td[subkey]
            return self

        del self._tensordict[key]
        if key in self._dict_meta:
            del self._dict_meta[key]
        return self

    def rename_key(
        self, old_key: str, new_key: str, safe: bool = False
    ) -> TensorDictBase:
        if not isinstance(old_key, str):
            raise TypeError(
                f"Expected old_name to be a string but found {type(old_key)}"
            )
        if not isinstance(new_key, str):
            raise TypeError(
                f"Expected new_name to be a string but found {type(new_key)}"
            )

        if safe and (new_key in self.keys()):
            raise KeyError(f"key {new_key} already present in TensorDict.")
        self.set(
            new_key,
            self.get(old_key),
            _meta_val=self._get_meta(old_key) if old_key in self._dict_meta else None,
            _run_checks=False,
        )
        self.del_(old_key)
        return self

    def set_(
        self, key: str, value: Union[dict, COMPATIBLE_TYPES], no_check: bool = False
    ) -> TensorDictBase:
        if not no_check:
            _nested_key_type_check(key)

        if no_check or key in self.keys(include_nested=True):
            if not no_check:
                proc_value = self._process_input(
                    value, check_device=False, check_shared=False
                )
                # copy_ will broadcast one tensor onto another's shape, which we don't want
                target_shape = self._get_meta(key).shape
                if proc_value.shape != target_shape:
                    raise RuntimeError(
                        f'calling set_("{key}", tensor) with tensors of '
                        f"different shape: got tensor.shape={proc_value.shape} "
                        f'and get("{key}").shape={target_shape}'
                    )
            else:
                proc_value = value
            if proc_value is not self.get(key):
                self.get(key).copy_(proc_value)
                if type(key) is tuple:
                    # If we have a nested key, we must traverse the nested tensordicts
                    # until we reach the parent of the leaf tensor, then check
                    # _dict_meta on that tensordict.
                    td, subkey = _get_leaf_tensordict(self, key)

                    if subkey in td._dict_meta:
                        td._dict_meta[subkey].requires_grad = proc_value.requires_grad

        else:
            raise AttributeError(
                f'key "{key}" not found in tensordict, '
                f'call td.set("{key}", value) for populating tensordict with '
                f"new key-value pair"
            )
        return self

    def _stack_onto_(
        self, key: str, list_item: List[COMPATIBLE_TYPES], dim: int
    ) -> TensorDict:
        torch.stack(list_item, dim=dim, out=self.get(key))
        return self

    def _stack_onto_at_(
        self,
        key: str,
        list_item: List[COMPATIBLE_TYPES],
        dim: int,
        idx: INDEX_TYPING,
    ) -> TensorDict:
        if isinstance(idx, tuple) and len(idx) == 1:
            idx = idx[0]
        if isinstance(idx, (int, slice)) or (
            isinstance(idx, tuple)
            and all(isinstance(_idx, (int, slice)) for _idx in idx)
        ):
            torch.stack(list_item, dim=dim, out=self._tensordict[key][idx])
        else:
            raise ValueError(
                f"Cannot stack onto an indexed tensor with index {idx} "
                f"as its storage differs."
            )
        return self

    def set_at_(
        self, key: NESTED_KEY, value: Union[dict, COMPATIBLE_TYPES], idx: INDEX_TYPING
    ) -> TensorDictBase:
        _nested_key_type_check(key)
        is_nested = type(key) is tuple
        # do we need this?
        if not isinstance(value, _accepted_classes):
            value = self._process_input(
                value, check_tensor_shape=False, check_device=False
            )
        if key not in self.keys(is_nested):
            raise KeyError(f"did not find key {key} in {self.__class__.__name__}")
        tensor_in = self.get(key)

        if isinstance(idx, tuple) and len(idx) and isinstance(idx[0], tuple):
            warn(
                "Multiple indexing can lead to unexpected behaviours when "
                "setting items, for instance `td[idx1][idx2] = other` may "
                "not write to the desired location if idx1 is a list/tensor."
            )
            tensor_in = _sub_index(tensor_in, idx)
            tensor_in.copy_(value)
        else:
            _set_item(tensor_in, value, idx)

        # change Meta in case of require_grad coming in value
        if (type(key) is str) and key in self._dict_meta:
            self._dict_meta[key].requires_grad = _requires_grad(tensor_in)
        elif type(key) is tuple:
            # If we have a nested key, we must traverse the nested tensordicts until we
            # reach the parent of the leaf tensor, then check _dict_meta on that
            td, subkey = _get_leaf_tensordict(self, key)

            if subkey in td._dict_meta:
                td._dict_meta[subkey].requires_grad = tensor_in.requires_grad
        return self

    def get(
        self, key: str, default: Union[str, COMPATIBLE_TYPES] = "_no_default_"
    ) -> COMPATIBLE_TYPES:
        _nested_key_type_check(key)

        try:
            if type(key) is tuple:
                if len(key) > 1:
                    first_lev = self.get(key[0])
                    if len(key) == 2 and isinstance(first_lev, KeyedJaggedTensor):
                        return first_lev[key[1]]
                    return first_lev.get(key[1:])
                return self.get(key[0])
            return self._tensordict[key]
        except KeyError:
            # this is slower than a if / else but (1) it allows to avoid checking
            # that the key is present and (2) it should be used less frequently than
            # the regular get()
            return self._default_get(key, default)

    def share_memory_(self) -> TensorDictBase:
        if self.is_memmap():
            raise RuntimeError(
                "memmap and shared memory are mutually exclusive features."
            )
        if self.device is not None and self.device.type == "cuda":
            # cuda tensors are shared by default
            return self
        for value in self.values():
            # no need to consider MemmapTensors here as we have checked that this is not a memmap-tensordict
            if (
                isinstance(value, torch.Tensor)
                and value.device.type == "cpu"
                or isinstance(value, TensorDictBase)
            ):
                value.share_memory_()
        for value in self.values_meta():
            value.share_memory_()
        self._is_shared = True
        self.lock()
        return self

    def detach_(self) -> TensorDictBase:
        for value in self.values():
            value.detach_()
        return self

    def memmap_(self, prefix=None) -> TensorDictBase:
        if self.is_shared() and self.device.type == "cpu":
            raise RuntimeError(
                "memmap and shared memory are mutually exclusive features."
            )
        if not self._tensordict.keys():
            raise Exception(
                "memmap_() must be called when the TensorDict is (partially) "
                "populated. Set a tensor first."
            )
        for key, value in self.items():
            if value.requires_grad:
                raise Exception(
                    "memmap is not compatible with gradients, one of Tensors has requires_grad equals True"
                )
            if isinstance(value, TensorDictBase):
                self._tensordict[key] = value.memmap_()
                continue
            self._tensordict[key] = MemmapTensor(value, prefix=prefix)
        for value in self.values_meta():
            value.memmap_()
        self._is_memmap = True
        self.lock()
        return self

    def to(
        self, dest: Union[DEVICE_TYPING, torch.Size, Type], **kwargs
    ) -> TensorDictBase:
        if isinstance(dest, type) and issubclass(dest, TensorDictBase):
            if isinstance(self, dest):
                return self
            td = dest(
                source=self,
                **kwargs,
            )
            return td
        elif isinstance(dest, (torch.device, str, int)):
            # must be device
            dest = torch.device(dest)
            if self.device is not None and dest == self.device:
                return self

            self_copy = TensorDict(
                {key: value.to(dest, **kwargs) for key, value in self.items()},
                batch_size=self.batch_size,
                device=dest,
            )
            return self_copy
        elif isinstance(dest, torch.Size):
            self.batch_size = dest
            return self
        else:
            raise NotImplementedError(
                f"dest must be a string, torch.device or a TensorDict "
                f"instance, {dest} not allowed"
            )

    def masked_fill_(
        self, mask: Tensor, value: Union[float, int, bool]
    ) -> TensorDictBase:
        for item in self.values():
            mask_expand = expand_as_right(mask, item)
            item.masked_fill_(mask_expand, value)
        return self

    def masked_fill(self, mask: Tensor, value: Union[float, bool]) -> TensorDictBase:
        td_copy = self.clone()
        return td_copy.masked_fill_(mask, value)

    def is_contiguous(self) -> bool:
        return all([value.is_contiguous() for _, value in self.items()])

    def contiguous(self) -> TensorDictBase:
        if not self.is_contiguous():
            return self.clone()
        return self

    def select(
        self, *keys: NESTED_KEY, inplace: bool = False, strict: bool = True
    ) -> TensorDictBase:
        # existing_keys = set(self.keys(include_nested=True))
        keys = {
            key[0] if (type(key) is tuple) and len(key) == 1 else key for key in keys
        }

        nested_keys = defaultdict(list)
        for key in keys:
            _nested_key_type_check(key)
            if type(key) is str:
                # ensure key is in the top level of the dict
                nested_keys[key]
            elif len(key) == 1:
                nested_keys[key[0]]
            else:
                nested_keys[key[0]].append(key[1:])

        d = {}
        d_meta = {}

        for key, subkeys in nested_keys.items():
            try:
                value = self.get(key)
                if len(subkeys) > 0 and isinstance(value, TensorDictBase):
                    value = value.select(*subkeys, inplace=inplace)
                    d_meta[key] = MetaTensor(value)
                elif key in self._dict_meta:
                    d_meta[key] = self._dict_meta[key]

                d[key] = value
            except KeyError:
                if strict:
                    raise KeyError(
                        f"Key '{key}' was not found among keys {set(self.keys(True))}."
                    )
                else:
                    continue

        if inplace:
            self._tensordict = d
            for key in list(self._dict_meta.keys()):
                if key not in nested_keys:
                    del self._dict_meta[key]
                elif len(nested_keys[key]) > 0:
                    # meta value needs to be updated as not all keys present in children
                    self._dict_meta[key] = d_meta[key]
            return self
        return TensorDict(
            device=self.device,
            batch_size=self.batch_size,
            source=d,
            _meta_source=d_meta,
            _run_checks=False,
            _is_memmap=self._is_memmap,
            _is_shared=self._is_shared,
        )

    def keys(
        self, include_nested: bool = False, leaves_only: bool = False
    ) -> _TensorDictKeysView:
        return _TensorDictKeysView(
            self, include_nested=include_nested, leaves_only=leaves_only
        )


class _ErrorInteceptor:
    """Context manager for catching errors and modifying message.

    Intended for use with stacking / concatenation operations applied to TensorDicts.

    """

    DEFAULT_EXC_MSG = "Expected all tensors to be on the same device"

    def __init__(
        self, key, prefix, exc_msg: str = None, exc_type: Type[Exception] = None
    ):
        self.exc_type = exc_type if exc_type is not None else RuntimeError
        self.exc_msg = exc_msg if exc_msg is not None else self.DEFAULT_EXC_MSG
        self.prefix = prefix
        self.key = key

    def _add_key_to_error_msg(self, msg: str) -> str:
        if msg.startswith(self.prefix):
            return f'{self.prefix} "{self.key}" /{msg[len(self.prefix):]}'
        return f'{self.prefix} "{self.key}". {msg}'

    def __enter__(self):
        pass

    def __exit__(self, exc_type, exc_value, _):
        if exc_type is self.exc_type and (
            self.exc_msg is None or self.exc_msg in str(exc_value)
        ):
            exc_value.args = (self._add_key_to_error_msg(str(exc_value)),)


def _nested_keys_to_dict(keys: Iterator[NESTED_KEY]) -> Dict[str, Any]:
    nested_keys = {}
    for key in keys:
        if type(key) is str:
            nested_keys.setdefault(key, {})
        else:
            d = nested_keys
            for subkey in key:
                d = d.setdefault(subkey, {})
    return nested_keys


def _dict_to_nested_keys(nested_keys, prefix=()):
    for key, subkeys in nested_keys.items():
        if subkeys:
            yield from _dict_to_nested_keys(subkeys, prefix=prefix + (key,))
        elif prefix:
            yield prefix + (key,)
        else:
            yield key


def _default_hook(td, k):
    if k[0] not in td.keys():
        td.set(k[0], td.select())


def _get_leaf_tensordict(tensordict: TensorDictBase, key: NESTED_KEY, hook=None):
    # utility function for traversing nested tensordicts
    while len(key) > 1:
        if hook is not None:
            hook(tensordict, key)
        tensordict = tensordict.get(key[0])
        key = key[1:]
    return tensordict, key[0]


def implements_for_td(torch_function: Callable) -> Callable:
    """Register a torch function override for TensorDict."""

    @functools.wraps(torch_function)
    def decorator(func):
        TD_HANDLED_FUNCTIONS[torch_function] = func
        return func

    return decorator


# @implements_for_td(torch.testing.assert_allclose) TODO
def assert_allclose_td(
    actual: TensorDictBase,
    expected: TensorDictBase,
    rtol: float = None,
    atol: float = None,
    equal_nan: bool = True,
    msg: str = "",
) -> bool:
    """Compares two tensordicts and raise an exception if their content does not match exactly."""
    if not isinstance(actual, TensorDictBase) or not isinstance(
        expected, TensorDictBase
    ):
        raise TypeError("assert_allclose inputs must be of TensorDict type")
    set1 = set(actual.keys())
    set2 = set(expected.keys())
    if not (len(set1.difference(set2)) == 0 and len(set2) == len(set1)):
        raise KeyError(
            "actual and expected tensordict keys mismatch, "
            f"keys {(set1 - set2).union(set2 - set1)} appear in one but not "
            f"the other."
        )
    keys = sorted(actual.keys(), key=str)
    for key in keys:
        input1 = actual.get(key)
        input2 = expected.get(key)
        if isinstance(input1, TensorDictBase):
            assert_allclose_td(input1, input2, rtol=rtol, atol=atol)
            continue

        mse = (input1.to(torch.float) - input2.to(torch.float)).pow(2).sum()
        mse = mse.div(input1.numel()).sqrt().item()

        default_msg = f"key {key} does not match, got mse = {mse:4.4f}"
        if len(msg):
            msg = "\t".join([default_msg, msg])
        else:
            msg = default_msg
        if isinstance(input1, MemmapTensor):
            input1 = input1._tensor
        if isinstance(input2, MemmapTensor):
            input2 = input2._tensor
        torch.testing.assert_close(
            input1, input2, rtol=rtol, atol=atol, equal_nan=equal_nan, msg=msg
        )
    return True


@implements_for_td(torch.unbind)
def _unbind(td: TensorDictBase, *args, **kwargs) -> Tuple[TensorDictBase, ...]:
    return td.unbind(*args, **kwargs)


@implements_for_td(torch.full_like)
def _full_like(td: TensorDictBase, fill_value, **kwargs) -> TensorDictBase:
    td_clone = td.clone()
    for key in td_clone.keys():
        td_clone.fill_(key, fill_value)
    if "dtype" in kwargs:
        raise ValueError("Cannot pass dtype to full_like with TensorDict")
    if "device" in kwargs:
        td_clone = td_clone.to(kwargs.pop("device"))
    if len(kwargs):
        raise RuntimeError(
            f"keyword arguments {list(kwargs.keys())} are not "
            f"supported with full_like with TensorDict"
        )
    return td_clone


@implements_for_td(torch.zeros_like)
def _zeros_like(td: TensorDictBase, **kwargs) -> TensorDictBase:
    td_clone = td.clone()
    for key in td_clone.keys():
        td_clone.fill_(key, 0.0)
    if "dtype" in kwargs:
        raise ValueError("Cannot pass dtype to full_like with TensorDict")
    if "device" in kwargs:
        td_clone = td_clone.to(kwargs.pop("device"))
    if len(kwargs):
        raise RuntimeError(
            f"keyword arguments {list(kwargs.keys())} are not "
            f"supported with full_like with TensorDict"
        )
    return td_clone


@implements_for_td(torch.ones_like)
def _ones_like(td: TensorDictBase, **kwargs) -> TensorDictBase:
    td_clone = td.clone()
    for key in td_clone.keys():
        td_clone.fill_(key, 1.0)
    if "device" in kwargs:
        td_clone = td_clone.to(kwargs.pop("device"))
    if len(kwargs):
        raise RuntimeError(
            f"keyword arguments {list(kwargs.keys())} are not "
            f"supported with full_like with TensorDict"
        )
    return td_clone


@implements_for_td(torch.clone)
def _clone(td: TensorDictBase, *args, **kwargs) -> TensorDictBase:
    return td.clone(*args, **kwargs)


@implements_for_td(torch.squeeze)
def _squeeze(td: TensorDictBase, *args, **kwargs) -> TensorDictBase:
    return td.squeeze(*args, **kwargs)


@implements_for_td(torch.unsqueeze)
def _unsqueeze(td: TensorDictBase, *args, **kwargs) -> TensorDictBase:
    return td.unsqueeze(*args, **kwargs)


@implements_for_td(torch.masked_select)
def _masked_select(td: TensorDictBase, *args, **kwargs) -> TensorDictBase:
    return td.masked_select(*args, **kwargs)


@implements_for_td(torch.permute)
def _permute(td: TensorDictBase, dims) -> TensorDictBase:
    return td.permute(*dims)


@implements_for_td(torch.cat)
def _cat(
    list_of_tensordicts: Sequence[TensorDictBase],
    dim: int = 0,
    device: DEVICE_TYPING = None,
    out: TensorDictBase = None,
) -> TensorDictBase:
    if not list_of_tensordicts:
        raise RuntimeError("list_of_tensordicts cannot be empty")
    if dim < 0:
        raise RuntimeError(
            f"negative dim in torch.dim(list_of_tensordicts, dim=dim) not "
            f"allowed, got dim={dim}"
        )

    batch_size = list(list_of_tensordicts[0].batch_size)
    if dim >= len(batch_size):
        raise RuntimeError(
            f"dim must be in the range 0 <= dim < len(batch_size), got dim"
            f"={dim} and batch_size={batch_size}"
        )
    batch_size[dim] = sum([td.batch_size[dim] for td in list_of_tensordicts])
    batch_size = torch.Size(batch_size)

    # check that all tensordict match
    keys = _check_keys(list_of_tensordicts, strict=True)
    if out is None:
        out = {}
        for key in keys:
            with _ErrorInteceptor(
                key, "Attempted to concatenate tensors on different devices at key"
            ):
                out[key] = torch.cat([td.get(key) for td in list_of_tensordicts], dim)

        return TensorDict(out, device=device, batch_size=batch_size, _run_checks=False)
    else:
        if out.batch_size != batch_size:
            raise RuntimeError(
                "out.batch_size and cat batch size must match, "
                f"got out.batch_size={out.batch_size} and batch_size"
                f"={batch_size}"
            )

        for key in keys:
            with _ErrorInteceptor(
                key, "Attempted to concatenate tensors on different devices at key"
            ):
                if isinstance(out, TensorDict):
                    torch.cat(
                        [td.get(key) for td in list_of_tensordicts],
                        dim,
                        out=out.get(key),
                    )
                else:
                    out.set_(
                        key, torch.cat([td.get(key) for td in list_of_tensordicts], dim)
                    )
        return out


@implements_for_td(torch.stack)
def _stack(
    list_of_tensordicts: Sequence[TensorDictBase],
    dim: int = 0,
    device: DEVICE_TYPING = None,
    out: TensorDictBase = None,
    strict=False,
    contiguous=False,
) -> TensorDictBase:
    if not list_of_tensordicts:
        raise RuntimeError("list_of_tensordicts cannot be empty")
    batch_size = list_of_tensordicts[0].batch_size
    if dim < 0:
        dim = len(batch_size) + dim + 1

    for td in list_of_tensordicts[1:]:
        if td.batch_size != list_of_tensordicts[0].batch_size:
            raise RuntimeError(
                "stacking tensordicts requires them to have congruent batch sizes, "
                f"got td1.batch_size={td.batch_size} and td2.batch_size="
                f"{list_of_tensordicts[0].batch_size}"
            )

    # check that all tensordict match
    keys = _check_keys(list_of_tensordicts)

    if out is None:
        device = list_of_tensordicts[0].device
        if contiguous:
            out = {}
            for key in keys:
                with _ErrorInteceptor(
                    key, "Attempted to stack tensors on different devices at key"
                ):
                    out[key] = torch.stack(
                        [_tensordict.get(key) for _tensordict in list_of_tensordicts],
                        dim,
                    )

            return TensorDict(
                out,
                batch_size=LazyStackedTensorDict._compute_batch_size(
                    batch_size, dim, len(list_of_tensordicts)
                ),
                device=device,
                _run_checks=False,
            )
        else:
            out = LazyStackedTensorDict(
                *list_of_tensordicts,
                stack_dim=dim,
            )
    else:
        batch_size = list(batch_size)
        batch_size.insert(dim, len(list_of_tensordicts))
        batch_size = torch.Size(batch_size)

        if out.batch_size != batch_size:
            raise RuntimeError(
                "out.batch_size and stacked batch size must match, "
                f"got out.batch_size={out.batch_size} and batch_size"
                f"={batch_size}"
            )

        out_keys = set(out.keys())
        if strict:
            in_keys = set(keys)
            if len(out_keys - in_keys) > 0:
                raise RuntimeError(
                    "The output tensordict has keys that are missing in the "
                    "tensordict that has to be written: {out_keys - in_keys}. "
                    "As per the call to `stack(..., strict=True)`, this "
                    "is not permitted."
                )
            elif len(in_keys - out_keys) > 0:
                raise RuntimeError(
                    "The resulting tensordict has keys that are missing in "
                    f"its destination: {in_keys - out_keys}. As per the call "
                    "to `stack(..., strict=True)`, this is not permitted."
                )

        for key in keys:
            if key in out_keys:
                out._stack_onto_(
                    key,
                    [_tensordict.get(key) for _tensordict in list_of_tensordicts],
                    dim,
                )
            else:
                with _ErrorInteceptor(
                    key, "Attempted to stack tensors on different devices at key"
                ):
                    out.set(
                        key,
                        torch.stack(
                            [
                                _tensordict.get(key)
                                for _tensordict in list_of_tensordicts
                            ],
                            dim,
                        ),
                        inplace=True,
                    )

    return out


def pad(tensordict: TensorDictBase, pad_size: Sequence[int], value: float = 0.0):
    """Pads all tensors in a tensordict along the batch dimensions with a constant value, returning a new tensordict.

    Args:
         tensordict (TensorDict): The tensordict to pad
         pad_size (Sequence[int]): The padding size by which to pad some batch
            dimensions of the tensordict, starting from the first dimension and
            moving forward. [len(pad_size) / 2] dimensions of the batch size will
            be padded. For example to pad only the first dimension, pad has the form
            (padding_left, padding_right). To pad two dimensions,
            (padding_left, padding_right, padding_top, padding_bottom) and so on.
            pad_size must be even and less than or equal to twice the number of batch dimensions.
         value (float, optional): The fill value to pad by, default 0.0

    Returns:
        A new TensorDict padded along the batch dimensions

    Examples:
        >>> from tensordict import TensorDict
        >>> from tensordict.tensordict import pad
        >>> import torch
        >>> td = TensorDict({'a': torch.ones(3, 4, 1),
        ...     'b': torch.ones(3, 4, 1, 1)}, batch_size=[3, 4])
        >>> dim0_left, dim0_right, dim1_left, dim1_right = [0, 1, 0, 2]
        >>> padded_td = pad(td, [dim0_left, dim0_right, dim1_left, dim1_right], value=0.0)
        >>> print(padded_td.batch_size)
        torch.Size([4, 6])
        >>> print(padded_td.get("a").shape)
        torch.Size([4, 6, 1])
        >>> print(padded_td.get("b").shape)
        torch.Size([4, 6, 1, 1])

    """
    if len(pad_size) > 2 * len(tensordict.batch_size):
        raise RuntimeError(
            "The length of pad_size must be <= 2 * the number of batch dimensions"
        )

    if len(pad_size) % 2:
        raise RuntimeError("pad_size must have an even number of dimensions")

    new_batch_size = list(tensordict.batch_size)
    for i in range(len(pad_size)):
        new_batch_size[i // 2] += pad_size[i]

    reverse_pad = pad_size[::-1]
    for i in range(0, len(reverse_pad), 2):
        reverse_pad[i], reverse_pad[i + 1] = reverse_pad[i + 1], reverse_pad[i]

    out = TensorDict({}, new_batch_size, device=tensordict.device, _run_checks=False)
    for key, tensor in tensordict.items():
        cur_pad = reverse_pad
        if len(pad_size) < len(_shape(tensor)) * 2:
            cur_pad = [0] * (len(_shape(tensor)) * 2 - len(pad_size)) + reverse_pad

        if isinstance(tensor, TensorDictBase):
            padded = pad(tensor, pad_size, value)
        else:
            padded = torch.nn.functional.pad(tensor, cur_pad, value=value)
        out.set(key, padded)

    return out


# @implements_for_td(torch.nn.utils.rnn.pad_sequence)
def pad_sequence_td(
    list_of_tensordicts: Sequence[TensorDictBase],
    batch_first: bool = True,
    padding_value: float = 0.0,
    out: TensorDictBase = None,
    device: Optional[DEVICE_TYPING] = None,
):
    if not list_of_tensordicts:
        raise RuntimeError("list_of_tensordicts cannot be empty")
    # check that all tensordict match
    keys = _check_keys(list_of_tensordicts)
    if out is None:
        out = TensorDict({}, [], device=device, _run_checks=False)
        for key in keys:
            out.set(
                key,
                torch.nn.utils.rnn.pad_sequence(
                    [td.get(key) for td in list_of_tensordicts],
                    batch_first=batch_first,
                    padding_value=padding_value,
                ),
            )
        return out
    else:
        for key in keys:
            out.set_(
                key,
                torch.nn.utils.rnn.pad_sequence(
                    [td.get(key) for td in list_of_tensordicts],
                    batch_first=batch_first,
                    padding_value=padding_value,
                ),
            )
        return out


@implements_for_td(torch.split)
def _split(td: TensorDict, split_size_or_sections: Union[int, List[int]], dim: int = 0):
    return td.split(split_size_or_sections, dim)


class SubTensorDict(TensorDictBase):
    """A TensorDict that only sees an index of the stored tensors.

    By default, indexing a tensordict with an iterable will result in a
    SubTensorDict. This is done such that a TensorDict indexed with
    non-contiguous index (e.g. a Tensor) will still point to the original
    memory location (unlike regular indexing of tensors).

    Examples:
        >>> from tensordict import TensorDict, SubTensorDict
        >>> source = {'random': torch.randn(3, 4, 5, 6),
        ...    'zeros': torch.zeros(3, 4, 1, dtype=torch.bool)}
        >>> batch_size = torch.Size([3, 4])
        >>> td = TensorDict(source, batch_size)
        >>> td_index = td[:, 2]
        >>> print(type(td_index), td_index.shape)
        <class 'tensordict.tensordict.TensorDict'> \
torch.Size([3])
        >>> td_index = td[slice(None), slice(None)]
        >>> print(type(td_index), td_index.shape)
        <class 'tensordict.tensordict.TensorDict'> \
torch.Size([3, 4])
        >>> td_index = td.get_sub_tensordict((slice(None), torch.tensor([0, 2], dtype=torch.long)))
        >>> print(type(td_index), td_index.shape)
        <class 'tensordict.tensordict.SubTensorDict'> \
torch.Size([3, 2])
        >>> _ = td_index.fill_('zeros', 1)
        >>> # the indexed tensors are updated with Trues
        >>> print(td.get('zeros'))
        tensor([[[ True],
                 [False],
                 [ True],
                 [False]],
        <BLANKLINE>
                [[ True],
                 [False],
                 [ True],
                 [False]],
        <BLANKLINE>
                [[ True],
                 [False],
                 [ True],
                 [False]]])

    """

    @classmethod
    def __new__(cls, *args, **kwargs):
        cls._safe = False
        cls._lazy = True
        cls._is_shared = False
        cls._is_memmap = False
        cls._inplace_set = True
        return TensorDictBase.__new__(cls)

    def __init__(
        self,
        source: TensorDictBase,
        idx: INDEX_TYPING,
        batch_size: Optional[Sequence[int]] = None,
    ):
        super().__init__()

        if not isinstance(source, TensorDictBase):
            raise TypeError(
                f"Expected source to be a subclass of TensorDictBase, "
                f"got {type(source)}"
            )
        self._source = source
        if not isinstance(idx, (tuple, list)):
            idx = (idx,)
        else:
            idx = tuple(idx)
        self.idx = idx
        self._batch_size = _getitem_batch_size(self._source.batch_size, self.idx)
        if batch_size is not None and batch_size != self.batch_size:
            raise RuntimeError("batch_size does not match self.batch_size.")

    def _make_meta(self, key: str) -> MetaTensor:
        out = self._source._get_meta(key)[self.idx]
        return out

    def exclude(self, *keys: str, inplace: bool = False) -> TensorDictBase:
        if inplace:
            return super().exclude(*keys, inplace=True)
        return TensorDict(
            {key: value for key, value in self.items()},
            batch_size=self.batch_size,
            device=self.device,
            _run_checks=False,
            _is_memmap=self.is_memmap(),
            _is_shared=self.is_shared(),
        ).exclude(*keys, inplace=True)

    @property
    def batch_size(self) -> torch.Size:
        return self._batch_size

    @batch_size.setter
    def batch_size(self, new_size: torch.Size):
        return self._batch_size_setter(new_size)

    @property
    def device(self) -> Union[None, torch.device]:
        return self._source.device

    @device.setter
    def device(self, value: DEVICE_TYPING) -> None:
        self._source.device = value

    def _preallocate(self, key: str, value: COMPATIBLE_TYPES) -> TensorDictBase:
        return self._source.set(key, value)

    def set(
        self,
        key: NESTED_KEY,
        tensor: Union[dict, COMPATIBLE_TYPES],
        inplace: bool = False,
        _run_checks: bool = True,
    ) -> TensorDictBase:
        is_nested = type(key) is tuple
        keys = self.keys(is_nested)
        if self.is_locked:
            if not inplace or key not in keys:
                raise RuntimeError(
                    "Cannot modify locked TensorDict. For in-place modification, consider using the `set_()` method."
                )
        if inplace and key in keys:
            return self.set_(key, tensor)
        elif key in keys:
            raise RuntimeError(
                "Calling `SubTensorDict.set(key, value, inplace=False)` is prohibited for existing tensors. "
                "Consider calling `SubTensorDict.set_(...)` or cloning your tensordict first."
            )

        tensor = self._process_input(
            tensor, check_device=False, check_tensor_shape=False
        )
        if isinstance(tensor, TensorDictBase) and tensor.batch_size != self.batch_size:
            tensor.batch_size = self.batch_size
        parent = self.get_parent_tensordict()

        if isinstance(tensor, TensorDictBase):
            tensor_expand = TensorDict(
                {
                    key: _expand_to_match_shape(
                        parent.batch_size, _tensor, self.batch_dims, self.device
                    )
                    for key, _tensor in tensor.items()
                },
                parent.batch_size,
                _run_checks=False,
            )
        else:
            tensor_expand = torch.zeros(
                *parent.batch_size,
                *_shape(tensor)[self.batch_dims :],
                dtype=tensor.dtype,
                device=self.device,
            )
            if self.is_shared() and self.device.type == "cpu":
                tensor_expand.share_memory_()
            elif self.is_memmap():
                tensor_expand = MemmapTensor(tensor_expand)
        parent.set(key, tensor_expand, _run_checks=_run_checks)
        self.set_(key, tensor)
        if (type(key) is str) and key in self._dict_meta:
            self._dict_meta[key].requires_grad = tensor.requires_grad
        elif type(key) is tuple:
            td, subkey = _get_leaf_tensordict(self, key)
            if subkey in td._dict_meta:
                td._dict_meta[subkey].requires_grad = tensor.requires_grad
        return self

    def keys(
        self, include_nested: bool = False, leaves_only: bool = False
    ) -> _TensorDictKeysView:
        # TODO: temporary hack while SavedTensorDict doesn't support nested iteration
        if isinstance(self._source, SavedTensorDict):
            include_nested = False
        return self._source.keys(include_nested=include_nested, leaves_only=leaves_only)

    def set_(
        self,
        key: NESTED_KEY,
        tensor: Union[dict, COMPATIBLE_TYPES],
        no_check: bool = False,
    ) -> SubTensorDict:
        is_nested = type(key) is tuple
        if not no_check:
            tensor = self._process_input(
                tensor, check_device=False, check_tensor_shape=False
            )
            if key not in self.keys(is_nested):
                raise KeyError(f"key {key} not found in {self.keys()}")
            if (
                not isinstance(tensor, dict)
                and _shape(tensor)[: self.batch_dims] != self.batch_size
            ):
                raise RuntimeError(
                    f"tensor.shape={_shape(tensor)[:self.batch_dims]} and "
                    f"self.batch_size={self.batch_size} mismatch"
                )

        self._source.set_at_(key, tensor, self.idx)
        if (type(key) is str) and key in self._dict_meta:
            self._dict_meta[key].requires_grad = tensor.requires_grad
        elif type(key) is tuple:
            td, subkey = _get_leaf_tensordict(self, key)
            if subkey in td._dict_meta:
                td._dict_meta[subkey].requires_grad = tensor.requires_grad

        return self

    def _stack_onto_(
        self, key: str, list_item: List[COMPATIBLE_TYPES], dim: int
    ) -> TensorDict:
        self._source._stack_onto_at_(key, list_item, dim=dim, idx=self.idx)
        return self

    def to(
        self, dest: Union[DEVICE_TYPING, torch.Size, Type], **kwargs
    ) -> TensorDictBase:
        if isinstance(dest, type) and issubclass(dest, TensorDictBase):
            if isinstance(self, dest):
                return self
            return dest(
                source=self.clone(),
            )
        elif isinstance(dest, (torch.device, str, int)):
            dest = torch.device(dest)
            # try:
            if self.device is not None and dest == self.device:
                return self
            td = self.to_tensordict().to(dest, **kwargs)
            # must be device
            return td

        elif isinstance(dest, torch.Size):
            self.batch_size = dest
            return self
        else:
            raise NotImplementedError(
                f"dest must be a string, torch.device or a TensorDict "
                f"instance, {dest} not allowed"
            )

    def _change_batch_size(self, new_size: torch.Size):
        if not hasattr(self, "_orig_batch_size"):
            self._orig_batch_size = self.batch_size
        elif self._orig_batch_size == new_size:
            del self._orig_batch_size
        self._batch_size = new_size

    def get(
        self,
        key: NESTED_KEY,
        default: Optional[Union[Tensor, str]] = "_no_default_",
    ) -> COMPATIBLE_TYPES:
        return self._source.get_at(key, self.idx, default=default)

    def set_at_(
        self,
        key: NESTED_KEY,
        value: Union[dict, COMPATIBLE_TYPES],
        idx: INDEX_TYPING,
        discard_idx_attr: bool = False,
    ) -> SubTensorDict:
        if not isinstance(idx, tuple):
            idx = (idx,)
        if not isinstance(value, _accepted_classes):
            value = self._process_input(
                value, check_tensor_shape=False, check_device=False
            )
        if discard_idx_attr:
            self._source.set_at_(key, value, idx)
        else:
            tensor = self._source.get_at(key, self.idx)
            tensor[idx] = value
            self._source.set_at_(key, tensor, self.idx)
        if (type(key) is str) and key in self._dict_meta:
            self._dict_meta[key].requires_grad = value.requires_grad
        elif type(key) is tuple:
            td, subkey = _get_leaf_tensordict(self, key)
            if subkey in td._dict_meta:
                td._dict_meta[subkey].requires_grad = value.requires_grad
        return self

    def get_at(
        self,
        key: str,
        idx: INDEX_TYPING,
        discard_idx_attr: bool = False,
        default: Optional[Union[Tensor, str]] = "_no_default_",
    ) -> COMPATIBLE_TYPES:
        if not isinstance(idx, tuple):
            idx = (idx,)
        if discard_idx_attr:
            return self._source.get_at(key, idx, default=default)
        else:
            out = self._source.get_at(key, self.idx, default=default)
            if out is default:
                return out
            return out[idx]

    def update(
        self,
        input_dict_or_td: Union[dict, TensorDictBase],
        clone: bool = False,
        inplace: bool = False,
        **kwargs,
    ):
        if input_dict_or_td is self:
            # no op
            return self
        keys = set(self.keys(False))
        for key, value in input_dict_or_td.items():
            if clone and hasattr(value, "clone"):
                value = value.clone()
            else:
                value = tree_map(torch.clone, value)
            if type(key) is tuple:
                key, subkey = key[0], key[1:]
            else:
                subkey = []
            # the key must be a string by now. Let's check if it is present
            if key in keys:
                target = self._get_meta(key)
                if target.is_tensordict():
                    target = self._source.get(key).get_sub_tensordict(self.idx)
                    if len(subkey):
                        target.update({subkey: value})
                        continue
                    elif isinstance(value, (dict, TensorDictBase)):
                        target.update(value)
                        continue
                    raise ValueError(
                        f"Tried to replace a tensordict with an incompatible object of type {type(value)}"
                    )
                else:
                    self.set_(key, value)
            else:
                self.set(key, value, inplace=inplace, **kwargs)
        return self

    def update_(
        self,
        input_dict: Union[Dict[str, COMPATIBLE_TYPES], TensorDictBase],
        clone: bool = False,
    ) -> SubTensorDict:
        return self.update_at_(
            input_dict, idx=self.idx, discard_idx_attr=True, clone=clone
        )

    def update_at_(
        self,
        input_dict: Union[Dict[str, COMPATIBLE_TYPES], TensorDictBase],
        idx: INDEX_TYPING,
        discard_idx_attr: bool = False,
        clone: bool = False,
    ) -> SubTensorDict:
        for key, value in input_dict.items():
            if not isinstance(value, _accepted_classes):
                raise TypeError(
                    f"Expected value to be one of types {_accepted_classes} "
                    f"but got {type(value)}"
                )
            if clone:
                value = value.clone()
            self.set_at_(
                key,
                value,
                idx,
                discard_idx_attr=discard_idx_attr,
            )
        return self

    def get_parent_tensordict(self) -> TensorDictBase:
        if not isinstance(self._source, TensorDictBase):
            raise TypeError(
                f"SubTensorDict was initialized with a source of type"
                f" {self._source.__class__.__name__}, "
                "parent tensordict not accessible"
            )
        return self._source

    def del_(self, key: str) -> TensorDictBase:
        self._source = self._source.del_(key)
        return self

    def clone(self, recurse: bool = True) -> SubTensorDict:
        if not recurse:
            return copy(self)
        return SubTensorDict(source=self._source, idx=self.idx)

    def is_contiguous(self) -> bool:
        return all([value.is_contiguous() for _, value in self.items()])

    def contiguous(self) -> TensorDictBase:
        if self.is_contiguous():
            return self
        return TensorDict(
            batch_size=self.batch_size,
            source={key: value for key, value in self.items()},
            device=self.device,
            _run_checks=False,
        )

    def select(
        self, *keys: str, inplace: bool = False, strict: bool = True
    ) -> TensorDictBase:
        if inplace:
            self._source = self._source.select(*keys, strict=strict)
            return self
        return self._source.select(*keys, strict=strict)[self.idx]

    def expand(self, *shape, inplace: bool = False) -> TensorDictBase:
        if len(shape) == 1 and isinstance(shape[0], Sequence):
            shape = tuple(shape[0])

        idx = self.idx
        if isinstance(idx, torch.Tensor) and idx.dtype is torch.double:
            # check that idx is not a mask, otherwise throw an error
            raise ValueError("Cannot expand a TensorDict masked using SubTensorDict")
        elif not isinstance(idx, tuple):
            # create an tuple idx with length equal to this TensorDict's number of dims
            idx = (idx,) + (slice(None),) * (self._source.ndimension() - 1)
        elif isinstance(idx, tuple) and len(idx) < self._source.ndimension():
            # create an tuple idx with length equal to this TensorDict's number of dims
            idx = idx + (slice(None),) * (self._source.ndimension() - len(idx))
        # now that idx has the same length as the source's number of dims, we can work with it

        source_shape = self._source.shape
        num_integer_types = 0
        for i in idx:
            if isinstance(i, (int, np.integer)) or (
                isinstance(i, torch.Tensor) and i.ndimension() == 0
            ):
                num_integer_types += 1
        number_of_extra_dim = len(source_shape) - len(shape) + num_integer_types
        if number_of_extra_dim > 0:
            new_source_shape = [shape[i] for i in range(number_of_extra_dim)]
            shape = shape[len(new_source_shape) :]
        else:
            new_source_shape = []
        new_idx = [slice(None) for _ in range(len(new_source_shape))]
        for _idx, _s in zip(idx, source_shape):
            # we're iterating through the source shape and the index
            # we want to get the new index and the new source shape

            if isinstance(_idx, (int, np.integer)) or (
                isinstance(_idx, torch.Tensor) and _idx.ndimension() == 0
            ):
                # if the index is an integer, do nothing, i.e. keep the index and the shape
                new_source_shape.append(_s)
                new_idx.append(_idx)
            elif _s == 1:
                # if the source shape at this dim is 1, expand that source dim to the size that is required
                new_idx.append(slice(None))
                new_source_shape.append(shape[0])
                shape = shape[1:]
            else:
                # in this case, the source shape must be different than 1. The index is going to be identical.
                new_idx.append(_idx)
                new_source_shape.append(shape[0])
                shape = shape[1:]
        assert not len(shape)
        new_source = self._source.expand(*new_source_shape)
        new_idx = tuple(new_idx)
        if inplace:
            self._source = new_source
            self.idx = new_idx
            self.batch_size = _getitem_batch_size(new_source_shape, new_idx)
        return new_source[new_idx]

    def is_shared(self) -> bool:
        return self._source.is_shared()

    def is_memmap(self) -> bool:
        return self._source.is_memmap()

    def rename_key(
        self, old_key: str, new_key: str, safe: bool = False
    ) -> SubTensorDict:
        self._source.rename_key(old_key, new_key, safe=safe)
        return self

    def pin_memory(self) -> TensorDictBase:
        self._source.pin_memory()
        return self

    def detach_(self) -> TensorDictBase:
        raise RuntimeError("Detaching a sub-tensordict in-place cannot be done.")

    def masked_fill_(self, mask: Tensor, value: Union[float, bool]) -> TensorDictBase:
        for key, item in self.items():
            self.set_(key, torch.full_like(item, value))
        return self

    def masked_fill(self, mask: Tensor, value: Union[float, bool]) -> TensorDictBase:
        td_copy = self.clone()
        return td_copy.masked_fill_(mask, value)

    def memmap_(self, prefix=None) -> TensorDictBase:
        raise RuntimeError(
            "Converting a sub-tensordict values to memmap cannot be done."
        )

    def share_memory_(self) -> TensorDictBase:
        raise RuntimeError(
            "Casting a sub-tensordict values to shared memory cannot be done."
        )


def merge_tensordicts(*tensordicts: TensorDictBase) -> TensorDictBase:
    """Merges tensordicts together."""
    if len(tensordicts) < 2:
        raise RuntimeError(
            f"at least 2 tensordicts must be provided, got" f" {len(tensordicts)}"
        )
    d = tensordicts[0].to_dict()
    batch_size = tensordicts[0].batch_size
    for td in tensordicts[1:]:
        d.update(td.to_dict())
        if td.batch_dims < len(batch_size):
            batch_size = td.batch_size
    return TensorDict(d, batch_size, device=td.device, _run_checks=False)


class _LazyStackedTensorDictKeysView(_TensorDictKeysView):
    def __len__(self):
        return len(self.tensordict.valid_keys)

    def _keys(self):
        return self.tensordict.valid_keys


class LazyStackedTensorDict(TensorDictBase):
    """A Lazy stack of TensorDicts.

    When stacking TensorDicts together, the default behaviour is to put them
    in a stack that is not instantiated.
    This allows to seamlessly work with stacks of tensordicts with operations
    that will affect the original tensordicts.

    Args:
         *tensordicts (TensorDict instances): a list of tensordict with
            same batch size.
         stack_dim (int): a dimension (between `-td.ndimension()` and
            `td.ndimension()-1` along which the stack should be performed.

    Examples:
        >>> from tensordict import TensorDict
        >>> import torch
        >>> tds = [TensorDict({'a': torch.randn(3, 4)}, batch_size=[3])
        ...     for _ in range(10)]
        >>> td_stack = torch.stack(tds, -1)
        >>> print(td_stack.shape)
        torch.Size([3, 10])
        >>> print(td_stack.get("a").shape)
        torch.Size([3, 10, 4])
        >>> print(td_stack[:, 0] is tds[0])
        True

    """

    _safe = False
    _lazy = True

    def __init__(
        self,
        *tensordicts: TensorDictBase,
        stack_dim: int = 0,
        batch_size: Optional[Sequence[int]] = None,  # TODO: remove
    ):
        super().__init__()

        self._is_shared = False
        self._is_memmap = False

        # sanity check
        N = len(tensordicts)
        if not N:
            raise RuntimeError(
                "at least one tensordict must be provided to "
                "StackedTensorDict to be instantiated"
            )
        if not isinstance(tensordicts[0], TensorDictBase):
            raise TypeError(
                f"Expected input to be TensorDictBase instance"
                f" but got {type(tensordicts[0])} instead."
            )
        if stack_dim < 0:
            raise RuntimeError(
                f"stack_dim must be non negative, got stack_dim={stack_dim}"
            )
        _batch_size = tensordicts[0].batch_size
        device = tensordicts[0].device

        for td in tensordicts[1:]:
            if not isinstance(td, TensorDictBase):
                raise TypeError(
                    "Expected all inputs to be TensorDictBase instances but got "
                    f"{type(td)} instead."
                )
            _bs = td.batch_size
            _device = td.device
            if device != _device:
                raise RuntimeError(f"devices differ, got {device} and {_device}")
            if _bs != _batch_size:
                raise RuntimeError(
                    f"batch sizes in tensordicts differs, StackedTensorDict "
                    f"cannot be created. Got td[0].batch_size={_batch_size} "
                    f"and td[i].batch_size={_bs} "
                )
        self.tensordicts: List[TensorDictBase] = list(tensordicts)
        self.stack_dim = stack_dim
        self._batch_size = self._compute_batch_size(_batch_size, stack_dim, N)
        self._update_valid_keys()
        if batch_size is not None and batch_size != self.batch_size:
            raise RuntimeError("batch_size does not match self.batch_size.")

    @property
    def device(self) -> Union[None, torch.device]:
        # devices might have changed, so we check that they're all the same
        device_set = {td.device for td in self.tensordicts}
        if len(device_set) != 1:
            raise RuntimeError(
                f"found multiple devices in {self.__class__.__name__}:" f" {device_set}"
            )
        device = self.tensordicts[0].device
        return device

    @device.setter
    def device(self, value: DEVICE_TYPING) -> None:
        for t in self.tensordicts:
            t.device = value

    @property
    def batch_size(self) -> torch.Size:
        return self._batch_size

    @batch_size.setter
    def batch_size(self, new_size: torch.Size):
        return self._batch_size_setter(new_size)

    def is_shared(self) -> bool:
        are_shared = [td.is_shared() for td in self.tensordicts]
        are_shared = [value for value in are_shared if value is not None]
        if not len(are_shared):
            return None
        if any(are_shared) and not all(are_shared):
            raise RuntimeError(
                f"tensordicts shared status mismatch, got {sum(are_shared)} "
                f"shared tensordicts and "
                f"{len(are_shared) - sum(are_shared)} non shared tensordict "
            )
        return all(are_shared)

    def is_memmap(self) -> bool:
        are_memmap = [td.is_memmap() for td in self.tensordicts]
        if any(are_memmap) and not all(are_memmap):
            raise RuntimeError(
                f"tensordicts memmap status mismatch, got {sum(are_memmap)} "
                f"memmap tensordicts and "
                f"{len(are_memmap) - sum(are_memmap)} non memmap tensordict "
            )
        return all(are_memmap)

    def get_valid_keys(self) -> Set[str]:
        if self._valid_keys is None:
            self._update_valid_keys()
        return self._valid_keys

    def set_valid_keys(self, keys: Sequence[str]) -> None:
        raise RuntimeError(
            "setting valid keys is not permitted. valid keys are defined as "
            "the intersection of all the key sets from the TensorDicts in a "
            "stack and cannot be defined explicitely."
        )

    valid_keys = property(get_valid_keys, set_valid_keys)

    @staticmethod
    def _compute_batch_size(
        batch_size: torch.Size, stack_dim: int, N: int
    ) -> torch.Size:
        s = list(batch_size)
        s.insert(stack_dim, N)
        return torch.Size(s)

    def set(
        self, key: NESTED_KEY, tensor: Union[dict, COMPATIBLE_TYPES], **kwargs
    ) -> TensorDictBase:

        if self.is_locked:
            raise RuntimeError(
                "Cannot modify locked TensorDict. For in-place modification, consider using the `set_()` method."
            )

        tensor = self._process_input(
            tensor, check_device=False, check_tensor_shape=False
        )
        if isinstance(tensor, TensorDictBase):
            if tensor.batch_size[: self.batch_dims] != self.batch_size:
                tensor.batch_size = self.clone(recurse=False).batch_size
        if self.batch_size != _shape(tensor)[: self.batch_dims]:
            raise RuntimeError(
                "Setting tensor to tensordict failed because the shapes "
                f"mismatch: got tensor.shape = {_shape(tensor)} and "
                f"tensordict.batch_size={self.batch_size}"
            )

        proc_tensor = tensor.unbind(self.stack_dim)
        for td, _item in zip(self.tensordicts, proc_tensor):
            td.set(key, _item, **kwargs)
        first_key = key if (type(key) is str) else key[0]
        if key not in self._valid_keys:
            self._valid_keys = sorted([*self._valid_keys, first_key], key=str)
        if first_key in self._dict_meta:
            del self._dict_meta[first_key]
        return self

    def set_(
        self, key: str, tensor: Union[dict, COMPATIBLE_TYPES], no_check: bool = False
    ) -> TensorDictBase:
        if not no_check:
            tensor = self._process_input(
                tensor,
                check_device=False,
                check_tensor_shape=False,
                check_shared=False,
            )
            if isinstance(tensor, TensorDictBase):
                if tensor.batch_size[: self.batch_dims] != self.batch_size:
                    tensor.batch_size = self.clone(recurse=False).batch_size
            if self.batch_size != _shape(tensor)[: self.batch_dims]:
                raise RuntimeError(
                    "Setting tensor to tensordict failed because the shapes "
                    f"mismatch: got tensor.shape = {_shape(tensor)} and "
                    f"tensordict.batch_size={self.batch_size}"
                )
            if key not in self.valid_keys:
                raise KeyError(
                    "setting a value in-place on a stack of TensorDict is only "
                    "permitted if all members of the stack have this key in "
                    "their register."
                )
        if key in self._dict_meta:
            self._dict_meta[key].requires_grad = tensor.requires_grad
        tensors = tensor.unbind(self.stack_dim)
        for td, _item in zip(self.tensordicts, tensors):
            td.set_(key, _item)
        return self

    def set_at_(
        self, key: str, value: Union[dict, COMPATIBLE_TYPES], idx: INDEX_TYPING
    ) -> TensorDictBase:
        sub_td = self[idx]
        sub_td.set_(key, value)
        return self

    def _stack_onto_(
        self,
        key: str,
        list_item: List[COMPATIBLE_TYPES],
        dim: int,
    ) -> TensorDictBase:
        if dim == self.stack_dim:
            for source, tensordict_dest in zip(list_item, self.tensordicts):
                tensordict_dest.set_(key, source)
        else:
            # we must stack and unbind, there is no way to make it more efficient
            self.set_(key, torch.stack(list_item, dim))
        return self

    def get(
        self,
        key: NESTED_KEY,
        default: Union[str, COMPATIBLE_TYPES] = "_no_default_",
    ) -> COMPATIBLE_TYPES:
        # TODO: the stacking logic below works for nested keys, but the key in
        # self.valid_keys check will fail and we'll return the default instead.
        # For now we'll advise user that nested keys aren't supported, but it should be
        # fairly easy to add support if we could add nested keys to valid_keys.

        # we can handle the case where the key is a tuple of length 1
        if (type(key) is tuple) and len(key) == 1:
            key = key[0]
        elif type(key) is tuple:
            tensordict, key = _get_leaf_tensordict(self, key)
            return tensordict[key]

        keys = self.valid_keys
        if not (key in keys):
            # first, let's try to update the valid keys
            self._update_valid_keys()
            keys = self.valid_keys

        if not (key in keys):
            return self._default_get(key, default)

        tensors = [td.get(key, default=default) for td in self.tensordicts]
        shapes = {_shape(tensor) for tensor in tensors}
        if len(shapes) != 1:
            raise RuntimeError(
                f"found more than one unique shape in the tensors to be "
                f"stacked ({shapes}). This is likely due to a modification "
                f"of one of the stacked TensorDicts, where a key has been "
                f"updated/created with an uncompatible shape."
            )
        return torch.stack(tensors, self.stack_dim)

    def _make_meta(self, key: str) -> MetaTensor:
        return torch.stack(
            [td._get_meta(key) for td in self.tensordicts], self.stack_dim
        )

    def is_contiguous(self) -> bool:
        return False

    def contiguous(self) -> TensorDictBase:
        source = {key: value for key, value in self.items()}
        batch_size = self.batch_size
        device = self.device
        out = TensorDict(
            source=source,
            batch_size=batch_size,
            # we could probably just infer the items_meta by extending them
            # _meta_source=meta_source,
            device=device,
            _run_checks=False,
        )
        return out

    def clone(self, recurse: bool = True) -> TensorDictBase:
        if recurse:
            # This could be optimized using copy but we must be careful with
            # metadata (_is_shared etc)
            return LazyStackedTensorDict(
                *[td.clone() for td in self.tensordicts],
                stack_dim=self.stack_dim,
            )
        return LazyStackedTensorDict(*self.tensordicts, stack_dim=self.stack_dim)

    def pin_memory(self) -> TensorDictBase:
        for td in self.tensordicts:
            td.pin_memory()
        return self

    def to(self, dest: Union[DEVICE_TYPING, Type], **kwargs) -> TensorDictBase:
        if isinstance(dest, type) and issubclass(dest, TensorDictBase):
            if isinstance(self, dest):
                return self
            kwargs.update({"batch_size": self.batch_size})
            return dest(source=self, **kwargs)
        elif isinstance(dest, (torch.device, str, int)):
            dest = torch.device(dest)
            if self.device is not None and dest == self.device:
                return self
            td = self.to_tensordict().to(dest, **kwargs)
            return td

        elif isinstance(dest, torch.Size):
            self.batch_size = dest
        else:
            raise NotImplementedError(
                f"dest must be a string, torch.device or a TensorDict "
                f"instance, {dest} not allowed"
            )

    def _check_new_batch_size(self, new_size: torch.Size):
        if len(new_size) <= self.stack_dim:
            raise RuntimeError(
                "Changing the batch_size of a LazyStackedTensorDicts can only "
                "be done with sizes that are at least as long as the "
                "stacking dimension."
            )
        super()._check_new_batch_size(new_size)

    def _change_batch_size(self, new_size: torch.Size):
        if not hasattr(self, "_orig_batch_size"):
            self._orig_batch_size = self.batch_size
        elif self._orig_batch_size == new_size:
            del self._orig_batch_size
        self._batch_size = new_size

    def keys(
        self, include_nested: bool = False, leaves_only: bool = False
    ) -> _LazyStackedTensorDictKeysView:
        keys = _LazyStackedTensorDictKeysView(
            self, include_nested=include_nested, leaves_only=leaves_only
        )
        return keys

    def _update_valid_keys(self) -> None:
        valid_keys = set(self.tensordicts[0].keys())
        for td in self.tensordicts[1:]:
            valid_keys = valid_keys.intersection(td.keys())
        self._valid_keys = sorted(valid_keys)

    def select(
        self, *keys: str, inplace: bool = False, strict: bool = None
    ) -> LazyStackedTensorDict:
        # TODO: Add support for nested keys.
        for key in keys:
            if not (type(key) is str):
                raise TypeError(
                    "All keys passed to LazyStackedTensorDict.select must be strings. "
                    f"Found {key} of type {type(key)}. Note that LazyStackedTensorDict "
                    "does not yet support nested keys."
                )
        # TODO: implement strict=True
        if strict:
            raise NotImplementedError(
                "strict=True is not yet implemented for LazyStackedTensorDict.select()"
            )
        # the following implementation keeps the hidden keys in the tensordicts
        excluded_keys = set(self.valid_keys) - set(keys)
        tensordicts = [
            td.exclude(*excluded_keys, inplace=inplace) for td in self.tensordicts
        ]
        if inplace:
            return self
        return LazyStackedTensorDict(
            *tensordicts,
            stack_dim=self.stack_dim,
        )

    def exclude(self, *keys: str, inplace: bool = False) -> LazyStackedTensorDict:
        tensordicts = [
            tensordict.exclude(*keys, inplace=inplace)
            for tensordict in self.tensordicts
        ]
        if inplace:
            self.tensordicts = tensordicts
            self._update_valid_keys()
            return self
        return torch.stack(tensordicts, dim=self.stack_dim)

    def __setitem__(self, item: INDEX_TYPING, value: TensorDictBase) -> TensorDictBase:
        if isinstance(item, list):
            item = torch.tensor(item, device=self.device)
        if isinstance(item, tuple) and any(
            isinstance(sub_index, list) for sub_index in item
        ):
            item = tuple(
                torch.tensor(sub_index, device=self.device)
                if isinstance(sub_index, list)
                else sub_index
                for sub_index in item
            )
        if (isinstance(item, Tensor) and item.dtype is torch.bool) or (
            isinstance(item, tuple)
            and any(
                isinstance(_item, Tensor) and _item.dtype is torch.bool
                for _item in item
            )
        ):
            raise RuntimeError(
                "setting values to a LazyStackTensorDict using boolean values is not supported yet."
                "If this feature is needed, feel free to raise an issue on github."
            )
        if isinstance(item, Tensor):
            # e.g. item.shape = [1, 2, 3] and stack_dim == 2
            if item.ndimension() >= self.stack_dim + 1:
                items = item.unbind(self.stack_dim)
                values = value.unbind(self.stack_dim)
                for td, _item, sub_td in zip(self.tensordicts, items, values):
                    td[_item] = sub_td
            else:
                values = value.unbind(self.stack_dim)
                for td, sub_td in zip(self.tensordicts, values):
                    td[item] = sub_td
            return self
        return super().__setitem__(item, value)

    def __contains__(self, item) -> bool:
        if isinstance(item, TensorDictBase):
            return any(item is td for td in self.tensordicts)
        super().__contains__(item)

    def __getitem__(self, item: INDEX_TYPING) -> TensorDictBase:
        if item is Ellipsis or (isinstance(item, tuple) and Ellipsis in item):
            item = convert_ellipsis_to_idx(item, self.batch_size)
        if isinstance(item, tuple) and sum(
            isinstance(_item, str) for _item in item
        ) not in [len(item), 0]:
            raise IndexError(_STR_MIXED_INDEX_ERROR)
        if isinstance(item, list):
            item = torch.tensor(item, device=self.device)
        if isinstance(item, tuple) and any(
            isinstance(sub_index, list) for sub_index in item
        ):
            item = tuple(
                torch.tensor(sub_index, device=self.device)
                if isinstance(sub_index, list)
                else sub_index
                for sub_index in item
            )
        if isinstance(item, str):
            return self.get(item)
        elif isinstance(item, tuple) and all(
            isinstance(sub_item, str) for sub_item in item
        ):
            out = self.get(item[0])
            if len(item) > 1:
                return out[item[1:]]
            else:
                return out
        elif isinstance(item, Tensor) and item.dtype == torch.bool:
            return self.masked_select(item)
        elif (
            isinstance(item, (Number,))
            or (isinstance(item, Tensor) and item.ndimension() == 0)
        ) and self.stack_dim == 0:
            return self.tensordicts[item]
        elif isinstance(item, (Tensor, list)) and self.stack_dim == 0:
            out = LazyStackedTensorDict(
                *[self.tensordicts[_item] for _item in item],
                stack_dim=self.stack_dim,
            )
            return out
        elif isinstance(item, (Tensor, list)) and self.stack_dim != 0:
            out = LazyStackedTensorDict(
                *[tensordict[item] for tensordict in self.tensordicts],
                stack_dim=self.stack_dim,
            )
            return out
        elif isinstance(item, slice) and self.stack_dim == 0:
            return LazyStackedTensorDict(
                *self.tensordicts[item], stack_dim=self.stack_dim
            )
        elif isinstance(item, slice) and self.stack_dim != 0:
            return LazyStackedTensorDict(
                *[tensordict[item] for tensordict in self.tensordicts],
                stack_dim=self.stack_dim,
            )
        elif isinstance(item, (slice, Number)):
            new_stack_dim = (
                self.stack_dim - 1 if isinstance(item, Number) else self.stack_dim
            )
            return LazyStackedTensorDict(
                *[td[item] for td in self.tensordicts],
                stack_dim=new_stack_dim,
            )
        elif isinstance(item, tuple):
            _sub_item = tuple(
                _item for i, _item in enumerate(item) if i == self.stack_dim
            )
            if len(_sub_item):
                tensordicts = self.tensordicts[_sub_item[0]]
                if isinstance(tensordicts, TensorDictBase):
                    return tensordicts
            else:
                tensordicts = self.tensordicts
            # select sub tensordicts
            _sub_item = tuple(
                _item for i, _item in enumerate(item) if i != self.stack_dim
            )
            if len(_sub_item):
                tensordicts = [td[_sub_item] for td in tensordicts]
            new_stack_dim = self.stack_dim - sum(
                [isinstance(_item, Number) for _item in item[: self.stack_dim]]
            )
            return torch.stack(list(tensordicts), dim=new_stack_dim)
        else:
            raise NotImplementedError(
                f"selecting StackedTensorDicts with type "
                f"{item.__class__.__name__} is not supported yet"
            )

    def del_(self, key: str, **kwargs) -> TensorDictBase:
        for td in self.tensordicts:
            td.del_(key, **kwargs)
        self._valid_keys.remove(key)
        return self

    def share_memory_(self) -> TensorDictBase:
        for td in self.tensordicts:
            td.share_memory_()
        self._is_shared = True
        self.lock()
        return self

    def detach_(self) -> TensorDictBase:
        for td in self.tensordicts:
            td.detach_()
        return self

    def memmap_(self, prefix=None) -> TensorDictBase:
        for td in self.tensordicts:
            td.memmap_(prefix=prefix)
        self._is_memmap = True
        self.lock()
        return self

    def expand(self, *shape, inplace: bool = False) -> TensorDictBase:
        if len(shape) == 1 and isinstance(shape[0], Sequence):
            shape = tuple(shape[0])
        stack_dim = len(shape) + self.stack_dim - self.ndimension()
        new_shape_tensordicts = [v for i, v in enumerate(shape) if i != stack_dim]
        tensordicts = [td.expand(*new_shape_tensordicts) for td in self.tensordicts]
        if inplace:
            self.tensordicts = tensordicts
            self.stack_dim = stack_dim
            return self
        return torch.stack(tensordicts, stack_dim)

    def update(
        self, input_dict_or_td: TensorDictBase, clone: bool = False, **kwargs
    ) -> TensorDictBase:
        if input_dict_or_td is self:
            # no op
            return self
        keys = set(self.keys(False))
        for key, value in input_dict_or_td.items():
            if clone and hasattr(value, "clone"):
                value = value.clone()
            else:
                value = tree_map(torch.clone, value)
            if type(key) is tuple:
                key, subkey = key[0], key[1:]
            else:
                subkey = ()
            # the key must be a string by now. Let's check if it is present
            if key in keys:
                target = self._get_meta(key)
                if target.is_tensordict():
                    if isinstance(value, dict):
                        value_unbind = TensorDict(
                            value, self.batch_size, _run_checks=False
                        ).unbind(self.stack_dim)
                    else:
                        value_unbind = value.unbind(self.stack_dim)
                    for t, _value in zip(self.tensordicts, value_unbind):
                        if len(subkey):
                            t.update({key: {subkey: _value}})
                        else:
                            t.update({key: _value})
                    continue
            self.set(key, value, **kwargs)
        self._update_valid_keys()
        return self

    def update_(
        self,
        input_dict_or_td: Union[Dict[str, COMPATIBLE_TYPES], TensorDictBase],
        clone: bool = False,
        **kwargs,
    ) -> TensorDictBase:
        if input_dict_or_td is self:
            # no op
            return self
        for key, value in input_dict_or_td.items():
            if not isinstance(value, _accepted_classes):
                raise TypeError(
                    f"Expected value to be one of types {_accepted_classes} "
                    f"but got {type(value)}"
                )
            if clone:
                value = value.clone()
            self.set_(key, value, **kwargs)
        return self

    def rename_key(
        self, old_key: str, new_key: str, safe: bool = False
    ) -> TensorDictBase:
        for td in self.tensordicts:
            td.rename_key(old_key, new_key, safe=safe)
        self._valid_keys = sorted(
            [key if key != old_key else new_key for key in self._valid_keys]
        )
        return self

    def masked_fill_(self, mask: Tensor, value: Union[float, bool]) -> TensorDictBase:
        mask_unbind = mask.unbind(dim=self.stack_dim)
        for _mask, td in zip(mask_unbind, self.tensordicts):
            td.masked_fill_(_mask, value)
        return self

    def masked_fill(self, mask: Tensor, value: Union[float, bool]) -> TensorDictBase:
        td_copy = self.clone()
        return td_copy.masked_fill_(mask, value)

    def insert(self, index: int, tensordict: TensorDictBase) -> None:
        """Insert a TensorDict into the stack at the specified index.

        Analogous to list.insert. The inserted TensorDict must have compatible
        batch_size and device. Insertion is in-place, nothing is returned.

        Args:
            index (int): The index at which the new TensorDict should be inserted.
            tensordict (TensorDictBase): The TensorDict to be inserted into the stack.

        """
        if not isinstance(tensordict, TensorDictBase):
            raise TypeError(
                "Expected new value to be TensorDictBase instance but got "
                f"{type(tensordict)} instead."
            )

        batch_size = self.tensordicts[0].batch_size
        device = self.tensordicts[0].device

        _batch_size = tensordict.batch_size
        _device = tensordict.device

        if device != _device:
            raise ValueError(
                f"Devices differ: stack has device={device}, new value has "
                f"device={_device}."
            )
        if _batch_size != batch_size:
            raise ValueError(
                f"Batch sizes in tensordicts differs: stack has "
                f"batch_size={batch_size}, new_value has batch_size={_batch_size}."
            )

        self.tensordicts.insert(index, tensordict)

        N = len(self.tensordicts)
        self._batch_size = self._compute_batch_size(batch_size, self.stack_dim, N)
        self._update_valid_keys()
        # recreate _dict_meta to ensure shapes of stacked tensors are correct
        self._dict_meta = KeyDependentDefaultDict(self._make_meta)

    def append(self, tensordict: TensorDictBase) -> None:
        """Append a TensorDict onto the stack.

        Analogous to list.append. The appended TensorDict must have compatible
        batch_size and device. The append operation is in-place, nothing is returned.

        Args:
            tensordict (TensorDictBase): The TensorDict to be appended onto the stack.

        """
        self.insert(len(self.tensordicts), tensordict)


class SavedTensorDict(TensorDictBase):
    """A saved tensordict class."""

    _safe = False
    _lazy = False

    def __init__(
        self,
        source: TensorDictBase,
        device: Optional[torch.device] = None,
        batch_size: Optional[Sequence[int]] = None,
    ):
        super().__init__()
        if not isinstance(source, TensorDictBase):
            raise TypeError(
                f"Expected source to be a TensorDictBase instance, but got {type(source)} instead."
            )
        elif isinstance(source, SavedTensorDict):
            source = source._load()
        if any(val.requires_grad for val in source.values()):
            raise Exception(
                "SavedTensorDicts is not compatible with gradients, one of Tensors has requires_grad equals True"
            )
        self.file = tempfile.NamedTemporaryFile()  # noqa: P201
        self.filename = self.file.name
        # if source.is_memmap():
        #     source = source.clone()
        self._device = torch.device(device) if device is not None else source.device
        self._save(source)
        if batch_size is not None and batch_size != self.batch_size:
            raise RuntimeError("batch_size does not match self.batch_size.")

    def _save(self, tensordict: TensorDictBase) -> None:
        self._version = uuid.uuid1()
        self._keys = list(tensordict.keys())
        self._batch_size = tensordict.batch_size
        self._td_fields = _td_fields(tensordict)
        self._dict_meta = {key: value for key, value in tensordict.items_meta()}
        torch.save(tensordict, self.filename)

    def _make_meta(self, key: str) -> MetaTensor:
        if key not in self._dict_meta:
            raise RuntimeError(
                f'the key "{key}" was not found in SavedTensorDict._dict_meta (keys: {self._dict_meta.keys()}.'
            )
        return self._dict_meta["key"]

    def _load(self) -> TensorDictBase:
        return torch.load(self.filename, map_location=self.device)

    @property
    def batch_size(self) -> torch.Size:
        return self._batch_size

    @batch_size.setter
    def batch_size(self, new_size: torch.Size):
        return self._batch_size_setter(new_size)

    def _batch_size_setter(self, new_size: torch.Size):
        td = self._load()
        td.batch_size = new_size
        self._save(td)
        return super()._batch_size_setter(new_size)

    @property
    def device(self) -> Union[None, torch.device]:
        return self._device

    @device.setter
    def device(self, value: DEVICE_TYPING) -> None:
        raise RuntimeError(
            "device cannot be set using tensordict.device = device, "
            "because device cannot be updated in-place. To update device, use "
            "tensordict.to(new_device), which will return a new tensordict "
            "on the new device."
        )

    def keys(
        self, include_nested: bool = False, leaves_only: bool = False
    ) -> Sequence[str]:
        # TODO: support iteration over nested keys
        if include_nested:
            raise ValueError(
                "SavedTensorDict does not currently support iteration over nested keys."
            )
        for k in self._keys:
            if leaves_only:
                meta_val = self._get_meta(k)
                if meta_val.is_tensordict():
                    continue
            yield k

    def get(
        self, key: NESTED_KEY, default: Union[str, COMPATIBLE_TYPES] = "_no_default_"
    ) -> COMPATIBLE_TYPES:
        td = self._load()
        return td.get(key, default=default)

    def set(
        self, key: NESTED_KEY, value: Union[dict, COMPATIBLE_TYPES], **kwargs
    ) -> TensorDictBase:
        if self.is_locked:
            # TODO: self.keys doesn't return nested keys currently, so this might fail
            # if tensordict is locked.
            raise RuntimeError(
                "Cannot modify locked TensorDict. For in-place modification, consider using the `set_()` method."
            )
        td = self._load()
        td.set(key, value, **kwargs)
        self._save(td)
        return self

    def set_default(
        self, key: NESTED_KEY, default: COMPATIBLE_TYPES, **kwargs
    ) -> COMPATIBLE_TYPES:
        """Insert key with a value of default if key is not in the dictionary.

        Return the value for key if key is in the dictionary, else default.

        Args:
            key (str): the name of the value.
            default (torch.Tensor): value to be stored in the tensordict if the key is
                not already present.

        Returns:
            The value of key in the tensordict. Will be default if the key was not
            previously set.

        """
        if type(key) is tuple:
            raise TypeError("SavedTensorDict does not currently support nested keys.")
        return super().set_default(key=key, default=default, **kwargs)

    def expand(self, *shape, inplace: bool = False) -> TensorDictBase:
        if len(shape) == 1 and isinstance(shape[0], Sequence):
            shape = tuple(shape[0])
        td = self._load()
        td = td.expand(*shape)
        if inplace:
            self._save(td)
            return self
        return td.to(SavedTensorDict)

    def _stack_onto_(
        self,
        key: str,
        list_item: List[COMPATIBLE_TYPES],
        dim: int,
    ) -> TensorDictBase:
        td = self._load()
        td._stack_onto_(key, list_item, dim)
        self._save(td)
        return self

    def set_(
        self, key: str, value: Union[dict, COMPATIBLE_TYPES], no_check: bool = False
    ) -> TensorDictBase:
        # TODO: self.keys() doesn't return nested keys
        if key not in self.keys():
            raise KeyError(f"key {key} not found in {self.keys()}")
        self.set(key, value)
        return self

    def set_at_(
        self, key: str, value: Union[dict, COMPATIBLE_TYPES], idx: INDEX_TYPING
    ) -> TensorDictBase:
        td = self._load()
        td.set_at_(key, value, idx)
        self._save(td)
        return self

    def update(
        self,
        input_dict_or_td: Union[Dict[str, COMPATIBLE_TYPES], TensorDictBase],
        clone: bool = False,
        **kwargs,
    ) -> TensorDictBase:
        if input_dict_or_td is self:
            # no op
            return self
        td = self._load()
        keys = set(td.keys(True))
        for key, value in input_dict_or_td.items():
            # if not isinstance(value, _accepted_classes):
            #     raise TypeError(
            #         f"Expected value to be one of types {_accepted_classes} "
            #         f"but got {type(value)}"
            #     )
            if clone and hasattr(value, clone):
                value = value.clone()
            if type(key) is tuple:
                key, subkey = key[0], key[1:]
            else:
                subkey = []
            # the key must be a string by now. Let's check if it is present
            if key in keys:
                target = self._get_meta(key)
                if target.is_tensordict():
                    target = self.get(key)
                    if len(subkey):
                        target.update({subkey: value})
                    else:
                        target.update(value)
                    continue
            td.set(key, value, inplace=False, **kwargs)
            if clone:
                value = value.clone()
            td.set(key, value, **kwargs)
        self._save(td)
        return self

    def update_(
        self,
        input_dict_or_td: Union[Dict[str, COMPATIBLE_TYPES], TensorDictBase],
        clone: bool = False,
    ) -> TensorDictBase:
        if input_dict_or_td is self:
            return self
        return self.update(input_dict_or_td, clone=clone)

    def __del__(self) -> None:
        if hasattr(self, "file"):
            self.file.close()

    def is_shared(self) -> bool:
        return False

    def is_memmap(self) -> bool:
        return False

    def share_memory_(self, lock=True) -> TensorDictBase:
        raise RuntimeError("SavedTensorDict cannot be put in shared memory.")

    def memmap_(self, prefix=None) -> TensorDictBase:
        raise RuntimeError(
            "SavedTensorDict and memmap are mutually exclusive features."
        )

    def detach_(self) -> TensorDictBase:
        raise RuntimeError("SavedTensorDict cannot be put detached.")

    def items(
        self, include_nested: bool = False, leaves_only: bool = False
    ) -> Iterator[Tuple[str, COMPATIBLE_TYPES]]:
        version = self._version
        for v in self._load().items(
            include_nested=include_nested, leaves_only=leaves_only
        ):
            if version != self._version:
                raise RuntimeError("The SavedTensorDict changed while querying items.")
            yield v

    def values(
        self, include_nested: bool = False, leaves_only: bool = False
    ) -> Iterator[COMPATIBLE_TYPES]:
        version = self._version
        for v in self._load().values(
            include_nested=include_nested, leaves_only=leaves_only
        ):
            if version != self._version:
                raise RuntimeError("The SavedTensorDict changed while querying values.")
            yield v

    def is_contiguous(self) -> bool:
        return False

    def contiguous(self) -> TensorDictBase:
        return self._load().contiguous()

    def clone(self, recurse: bool = True) -> TensorDictBase:
        return SavedTensorDict(self, device=self.device)

    def select(
        self, *keys: str, inplace: bool = False, strict: bool = True
    ) -> TensorDictBase:
        _source = self.contiguous().select(*keys, strict=strict)
        if inplace:
            self._save(_source)
            return self
        return SavedTensorDict(source=_source)

    def rename_key(
        self, old_key: str, new_key: str, safe: bool = False
    ) -> TensorDictBase:
        td = self._load()
        td.rename_key(old_key, new_key, safe=safe)
        self._save(td)
        return self

    def __repr__(self) -> str:
        return (
            f"SavedTensorDict(\n\tfields={{{self._td_fields}}}, \n\t"
            f"batch_size={self.batch_size}, \n\tfile={self.filename})"
        )

    def to(self, dest: Union[DEVICE_TYPING, Type], **kwargs):
        if isinstance(dest, type) and issubclass(dest, TensorDictBase):
            if isinstance(self, dest):
                return self
            kwargs.update({"batch_size": self.batch_size})
            td = dest(
                source=self.to_dict(),
                **kwargs,
            )
            return td
        elif isinstance(dest, (torch.device, str, int)):
            # must be device
            dest = torch.device(dest)
            if self.device is not None and dest == self.device:
                return self
            self_copy = copy(self)
            self_copy._device = dest
            self_copy._dict_meta = {
                key: value.to(dest) for key, value in self._dict_meta.items()
            }
            return self_copy
        if isinstance(dest, torch.Size):
            self.batch_size = dest
        else:
            raise NotImplementedError(
                f"dest must be a string, torch.device or a TensorDict "
                f"instance, {dest} not allowed"
            )

    def to_tensordict(self):
        """Returns a regular TensorDict instance from the TensorDictBase.

        Makes a copy of the tensor dict.

        Memmap and shared memory tensors are converted to regular tensors.

        Returns:
            a new TensorDict object containing the same values.

        """
        td = self._load()
        return TensorDict(
            {
                key: value.clone()
                if not isinstance(value, TensorDictBase)
                else value.to_tensordict()
                for key, value in td.items()
            },
            device=self.device,
            batch_size=self.batch_size,
            _run_checks=False,
        )

    def _change_batch_size(self, new_size: torch.Size):
        if not hasattr(self, "_orig_batch_size"):
            self._orig_batch_size = self.batch_size
        elif self._orig_batch_size == new_size:
            del self._orig_batch_size
        self._batch_size = new_size

    def del_(self, key: str) -> TensorDictBase:
        td = self._load()
        td = td.del_(key)
        self._save(td)
        return self

    def pin_memory(self) -> TensorDictBase:
        raise RuntimeError("pin_memory requires tensordicts that live in memory.")

    def __reduce__(self, *args, **kwargs):
        if hasattr(self, "file"):
            file = self.file
            del self.file
            self_copy = copy(self)
            self.file = file
            return super(SavedTensorDict, self_copy).__reduce__(*args, **kwargs)
        return super().__reduce__(*args, **kwargs)

    def __getitem__(self, idx: INDEX_TYPING) -> TensorDictBase:
        if isinstance(idx, list):
            idx = torch.tensor(idx, device=self.device)
        if isinstance(idx, tuple) and any(
            isinstance(sub_index, list) for sub_index in idx
        ):
            idx = tuple(
                torch.tensor(sub_index, device=self.device)
                if isinstance(sub_index, list)
                else sub_index
                for sub_index in idx
            )
        if idx is Ellipsis or (isinstance(idx, tuple) and Ellipsis in idx):
            idx = convert_ellipsis_to_idx(idx, self.batch_size)

        if isinstance(idx, tuple) and sum(
            isinstance(_idx, str) for _idx in idx
        ) not in [len(idx), 0]:
            raise IndexError(_STR_MIXED_INDEX_ERROR)

        if isinstance(idx, str):
            return self.get(idx)
        elif isinstance(idx, tuple) and all(
            isinstance(sub_idx, str) for sub_idx in idx
        ):
            out = self.get(idx[0])
            if len(idx) > 1:
                return out[idx[1:]]
            else:
                return out
        elif isinstance(idx, Number):
            idx = (idx,)
        elif isinstance(idx, Tensor) and idx.dtype == torch.bool:
            return self.masked_select(idx)
        if not self.batch_size:
            raise IndexError(
                "indexing a tensordict with td.batch_dims==0 is not permitted"
            )
        return self.get_sub_tensordict(idx)

    def masked_fill_(self, mask: Tensor, value: Union[float, bool]) -> TensorDictBase:
        td = self._load()
        td.masked_fill_(mask, value)
        self._save(td)
        return self

    def masked_fill(self, mask: Tensor, value: Union[float, bool]) -> TensorDictBase:
        td_copy = self.clone()
        return td_copy.masked_fill_(mask, value)


class _CustomOpTensorDict(TensorDictBase):
    """Encodes lazy operations on tensors contained in a TensorDict."""

    _lazy = True

    def __init__(
        self,
        source: TensorDictBase,
        custom_op: str,
        inv_op: Optional[str] = None,
        custom_op_kwargs: Optional[dict] = None,
        inv_op_kwargs: Optional[dict] = None,
        batch_size: Optional[Sequence[int]] = None,
    ):
        super().__init__()

        self._is_shared = source.is_shared()
        self._is_memmap = source.is_memmap()

        if not isinstance(source, TensorDictBase):
            raise TypeError(
                f"Expected source to be a TensorDictBase isntance, "
                f"but got {type(source)} instead."
            )
        self._source = source
        self.custom_op = custom_op
        self.inv_op = inv_op
        self.custom_op_kwargs = custom_op_kwargs if custom_op_kwargs is not None else {}
        self.inv_op_kwargs = inv_op_kwargs if inv_op_kwargs is not None else {}
        self._batch_size = None
        if batch_size is not None and batch_size != self.batch_size:
            raise RuntimeError("batch_size does not match self.batch_size.")

    def _update_custom_op_kwargs(
        self, source_meta_tensor: MetaTensor
    ) -> Dict[str, Any]:
        """Allows for a transformation to be customized for a certain shape, device or dtype.

        By default, this is a no-op on self.custom_op_kwargs

        Args:
            source_meta_tensor: corresponding MetaTensor

        Returns:
            a dictionary with the kwargs of the operation to execute
            for the tensor

        """
        return self.custom_op_kwargs

    def _update_inv_op_kwargs(self, source_tensor: Tensor) -> Dict[str, Any]:
        """Allows for an inverse transformation to be customized for a certain shape, device or dtype.

        By default, this is a no-op on self.inv_op_kwargs

        Args:
            source_tensor: corresponding tensor

        Returns:
            a dictionary with the kwargs of the operation to execute for
            the tensor

        """
        return self.inv_op_kwargs

    @property
    def device(self) -> Union[None, torch.device]:
        return self._source.device

    @device.setter
    def device(self, value: DEVICE_TYPING) -> None:
        self._source.device = value

    def _make_meta(self, key: str) -> MetaTensor:
        item = self._source._get_meta(key)
        return getattr(item, self.custom_op)(**self._update_custom_op_kwargs(item))

    def _get_meta(self, key) -> MetaTensor:
        return self._make_meta(key)

    @property
    def batch_size(self) -> torch.Size:
        if self._batch_size is None:
            self._batch_size = getattr(
                MetaTensor(*self._source.batch_size), self.custom_op
            )(**self.custom_op_kwargs).shape
        return self._batch_size

    @batch_size.setter
    def batch_size(self, new_size: torch.Size):
        return self._batch_size_setter(new_size)

    def _change_batch_size(self, new_size: torch.Size):
        if not hasattr(self, "_orig_batch_size"):
            self._orig_batch_size = self.batch_size
        elif self._orig_batch_size == new_size:
            del self._orig_batch_size
        self._batch_size = new_size

    def get(
        self,
        key: NESTED_KEY,
        default: Union[str, COMPATIBLE_TYPES] = "_no_default_",
        _return_original_tensor: bool = False,
    ) -> COMPATIBLE_TYPES:
        # TODO: temporary hack while SavedTensorDict and LazyStackedTensorDict don't
        # support nested iteration
        if isinstance(self._source, (LazyStackedTensorDict, SavedTensorDict)):
            include_nested = False
        else:
            include_nested = True

        if key in self._source.keys(include_nested=include_nested):
            source_meta_tensor = self._source._get_meta(key)
            item = self._source.get(key)
            transformed_tensor = getattr(item, self.custom_op)(
                **self._update_custom_op_kwargs(source_meta_tensor)
            )
            if not _return_original_tensor:
                return transformed_tensor
            return transformed_tensor, item
        else:
            if _return_original_tensor:
                raise RuntimeError(
                    "_return_original_tensor not compatible with get(..., "
                    "default=smth)"
                )
            return self._default_get(key, default)

    def set(
        self, key: str, value: Union[dict, COMPATIBLE_TYPES], **kwargs
    ) -> TensorDictBase:
        if self.inv_op is None:
            raise Exception(
                f"{self.__class__.__name__} does not support setting values. "
                f"Consider calling .contiguous() before calling this method."
            )
        if self.is_locked:
            raise RuntimeError(
                "Cannot modify locked TensorDict. For in-place modification, consider using the `set_()` method."
            )
        proc_value = self._process_input(
            value,
            check_device=False,
            check_tensor_shape=True,
        )
        proc_value = getattr(proc_value, self.inv_op)(
            **self._update_inv_op_kwargs(proc_value)
        )
        self._source.set(key, proc_value, **kwargs)
        return self

    def set_(
        self, key: str, value: Union[dict, COMPATIBLE_TYPES], no_check: bool = False
    ) -> _CustomOpTensorDict:
        if not no_check:
            if self.inv_op is None:
                raise Exception(
                    f"{self.__class__.__name__} does not support setting values. "
                    f"Consider calling .contiguous() before calling this method."
                )
            value = self._process_input(
                value,
                check_device=False,
                check_tensor_shape=True,
            )

        value = getattr(value, self.inv_op)(**self._update_inv_op_kwargs(value))
        self._source.set_(key, value)
        return self

    def set_at_(
        self, key: str, value: Union[dict, COMPATIBLE_TYPES], idx: INDEX_TYPING
    ) -> _CustomOpTensorDict:
        transformed_tensor, original_tensor = self.get(
            key, _return_original_tensor=True
        )
        if transformed_tensor.data_ptr() != original_tensor.data_ptr():
            raise RuntimeError(
                f"{self} original tensor and transformed_in do not point to the "
                f"same storage. Setting values in place is not currently "
                f"supported in this setting, consider calling "
                f"`td.clone()` before `td.set_at_(...)`"
            )
        if not isinstance(value, _accepted_classes):
            value = self._process_input(
                value, check_tensor_shape=False, check_device=False
            )
        transformed_tensor[idx] = value
        return self

    def _stack_onto_(
        self,
        key: str,
        list_item: List[COMPATIBLE_TYPES],
        dim: int,
    ) -> TensorDictBase:
        raise RuntimeError(
            f"stacking tensordicts is not allowed for type {type(self)}"
            f"consider calling 'to_tensordict()` first"
        )

    def __repr__(self) -> str:
        custom_op_kwargs_str = ", ".join(
            [f"{key}={value}" for key, value in self.custom_op_kwargs.items()]
        )
        indented_source = textwrap.indent(f"source={self._source}", "\t")
        return (
            f"{self.__class__.__name__}(\n{indented_source}, "
            f"\n\top={self.custom_op}({custom_op_kwargs_str}))"
        )

    def keys(
        self, include_nested: bool = False, leaves_only: bool = False
    ) -> _TensorDictKeysView:
        return self._source.keys(include_nested=include_nested, leaves_only=leaves_only)

    def select(
        self, *keys: str, inplace: bool = False, strict: bool = True
    ) -> _CustomOpTensorDict:
        if inplace:
            self._source.select(*keys, inplace=inplace, strict=strict)
            return self
        self_copy = copy(self)
        self_copy._source = self_copy._source.select(*keys, strict=strict)
        return self_copy

    def exclude(self, *keys: str, inplace: bool = False) -> TensorDictBase:
        if inplace:
            return super().exclude(*keys, inplace=True)
        return TensorDict(
            {key: value.clone() for key, value in self.items()},
            batch_size=self.batch_size,
            device=self.device,
            _run_checks=False,
            _is_memmap=self.is_memmap(),
            _is_shared=self.is_shared(),
        ).exclude(*keys, inplace=True)

    def clone(self, recurse: bool = True) -> TensorDictBase:
        if not recurse:
            return copy(self)
        return TensorDict(
            source=self.to_dict(),
            batch_size=self.batch_size,
            device=self.device,
            _run_checks=False,
        )

    def is_contiguous(self) -> bool:
        return all([value.is_contiguous() for _, value in self.items()])

    def contiguous(self) -> TensorDictBase:
        if self.is_contiguous():
            return self
        return self.to(TensorDict)

    def rename_key(
        self, old_key: str, new_key: str, safe: bool = False
    ) -> _CustomOpTensorDict:
        self._source.rename_key(old_key, new_key, safe=safe)
        return self

    def del_(self, key: str) -> _CustomOpTensorDict:
        self._source = self._source.del_(key)
        return self

    def to(self, dest: Union[DEVICE_TYPING, Type], **kwargs) -> TensorDictBase:
        if isinstance(dest, type) and issubclass(dest, TensorDictBase):
            if isinstance(self, dest):
                return self
            return dest(source=self)
        elif isinstance(dest, (torch.device, str, int)):
            if self.device is not None and torch.device(dest) == self.device:
                return self
            td = self._source.to(dest, **kwargs)
            self_copy = copy(self)
            self_copy._source = td
            return self_copy
        else:
            raise NotImplementedError(
                f"dest must be a string, torch.device or a TensorDict "
                f"instance, {dest} not allowed"
            )

    def pin_memory(self) -> TensorDictBase:
        self._source.pin_memory()
        return self

    def detach_(self):
        self._source.detach_()

    def masked_fill_(self, mask: Tensor, value: Union[float, bool]) -> TensorDictBase:
        for key, item in self.items():
            # source_meta_tensor = self._get_meta(key)
            val = self._source.get(key)
            mask_exp = expand_right(
                mask, list(mask.shape) + list(val.shape[self._source.batch_dims :])
            )
            mask_proc_inv = getattr(mask_exp, self.inv_op)(
                **self._update_inv_op_kwargs(item)
            )
            val[mask_proc_inv] = value
            self._source.set(key, val)
        return self

    def masked_fill(self, mask: Tensor, value: Union[float, bool]) -> TensorDictBase:
        td_copy = self.clone()
        return td_copy.masked_fill_(mask, value)

    def memmap_(self, prefix=None):
        self._source.memmap_(prefix=prefix)
        self._is_memmap = True
        self.lock()
        return self

    def share_memory_(self):
        self._source.share_memory_()
        self._is_shared = True
        self.lock()


class _UnsqueezedTensorDict(_CustomOpTensorDict):
    """A lazy view on an unsqueezed TensorDict.

    When calling `tensordict.unsqueeze(dim)`, a lazy view of this operation is
    returned such that the following code snippet works without raising an
    exception:

        >>> assert tensordict.unsqueeze(dim).squeeze(dim) is tensordict

    Examples:
        >>> from tensordict import TensorDict
        >>> import torch
        >>> td = TensorDict({'a': torch.randn(3, 4)}, batch_size=[3])
        >>> td_unsqueeze = td.unsqueeze(-1)
        >>> print(td_unsqueeze.shape)
        torch.Size([3, 1])
        >>> print(td_unsqueeze.squeeze(-1) is td)
        True
    """

    def squeeze(self, dim: Optional[int]) -> TensorDictBase:
        if dim is not None and dim < 0:
            dim = self.batch_dims + dim
        if dim == self.custom_op_kwargs.get("dim"):
            return self._source
        return super().squeeze(dim)

    def _stack_onto_(
        self,
        key: str,
        list_item: List[COMPATIBLE_TYPES],
        dim: int,
    ) -> TensorDictBase:
        unsqueezed_dim = self.custom_op_kwargs["dim"]
        diff_to_apply = 1 if dim < unsqueezed_dim else 0
        list_item_unsqueeze = [
            item.squeeze(unsqueezed_dim - diff_to_apply) for item in list_item
        ]
        return self._source._stack_onto_(key, list_item_unsqueeze, dim)


class _SqueezedTensorDict(_CustomOpTensorDict):
    """A lazy view on a squeezed TensorDict.

    See the `UnsqueezedTensorDict` class documentation for more information.

    """

    def unsqueeze(self, dim: int) -> TensorDictBase:
        if dim < 0:
            dim = self.batch_dims + dim + 1
        inv_op_dim = self.inv_op_kwargs.get("dim")
        if inv_op_dim < 0:
            inv_op_dim = self.batch_dims + inv_op_dim + 1
        if dim == inv_op_dim:
            return self._source
        return super().unsqueeze(dim)

    def _stack_onto_(
        self,
        key: str,
        list_item: List[COMPATIBLE_TYPES],
        dim: int,
    ) -> TensorDictBase:
        squeezed_dim = self.custom_op_kwargs["dim"]
        # dim=0, squeezed_dim=2, [3, 4, 5] [3, 4, 1, 5] [[4, 5], [4, 5], [4, 5]] => unsq 1
        # dim=1, squeezed_dim=2, [3, 4, 5] [3, 4, 1, 5] [[3, 5], [3, 5], [3, 5], [3, 4]] => unsq 1
        # dim=2, squeezed_dim=2, [3, 4, 5] [3, 4, 1, 5] [[3, 4], [3, 4], ...] => unsq 2
        diff_to_apply = 1 if dim < squeezed_dim else 0
        list_item_unsqueeze = [
            item.unsqueeze(squeezed_dim - diff_to_apply) for item in list_item
        ]
        return self._source._stack_onto_(key, list_item_unsqueeze, dim)


class _ViewedTensorDict(_CustomOpTensorDict):
    def _update_custom_op_kwargs(
        self, source_meta_tensor: MetaTensor
    ) -> Dict[str, Any]:
        new_dim_list = list(self.custom_op_kwargs.get("size"))
        new_dim_list += list(source_meta_tensor.shape[self._source.batch_dims :])
        new_dim = torch.Size(new_dim_list)
        new_dict = deepcopy(self.custom_op_kwargs)
        new_dict.update({"size": new_dim})
        return new_dict

    def _update_inv_op_kwargs(self, tensor: Tensor) -> Dict:
        size = list(self.inv_op_kwargs.get("size"))
        size += list(_shape(tensor)[self.batch_dims :])
        new_dim = torch.Size(size)
        new_dict = deepcopy(self.inv_op_kwargs)
        new_dict.update({"size": new_dim})
        return new_dict

    def view(
        self, *shape, size: Optional[Union[List, Tuple, torch.Size]] = None
    ) -> TensorDictBase:
        if len(shape) == 0 and size is not None:
            return self.view(*size)
        elif len(shape) == 1 and isinstance(shape[0], (list, tuple, torch.Size)):
            return self.view(*shape[0])
        elif not isinstance(shape, torch.Size):
            shape = infer_size_impl(shape, self.numel())
            shape = torch.Size(shape)
        if shape == self._source.batch_size:
            return self._source
        return super().view(*shape)


class _PermutedTensorDict(_CustomOpTensorDict):
    """A lazy view on a TensorDict with the batch dimensions permuted.

    When calling `tensordict.permute(dims_list, dim)`, a lazy view of this operation is
    returned such that the following code snippet works without raising an
    exception:

        >>> assert tensordict.permute(dims_list, dim).permute(dims_list, dim) is tensordict

    Examples:
        >>> from tensordict import TensorDict
        >>> import torch
        >>> td = TensorDict({'a': torch.randn(4, 5, 6, 9)}, batch_size=[3])
        >>> td_permute = td.permute(dims=(2, 1, 0))
        >>> print(td_permute.shape)
        torch.Size([6, 5, 4])
        >>> print(td_permute.permute(dims=(2, 1, 0)) is td)
        True

    """

    def permute(
        self,
        *dims_list: int,
        dims=None,
    ) -> TensorDictBase:
        if len(dims_list) == 0:
            dims_list = dims
        elif len(dims_list) == 1 and not isinstance(dims_list[0], int):
            dims_list = dims_list[0]
        if len(dims_list) != len(self.shape):
            raise RuntimeError(
                f"number of dims don't match in permute (got {len(dims_list)}, expected {len(self.shape)}"
            )
        if not len(dims_list) and not self.batch_dims:
            return self
        if np.array_equal(dims_list, range(self.batch_dims)):
            return self
        if np.array_equal(np.argsort(dims_list), self.inv_op_kwargs.get("dims")):
            return self._source
        return super().permute(*dims_list)

    def add_missing_dims(self, num_dims: int, batch_dims: Tuple[int]) -> Tuple[int]:
        dim_diff = num_dims - len(batch_dims)
        all_dims = list(range(num_dims))
        for i, x in enumerate(batch_dims):
            if x < 0:
                x = x - dim_diff
            all_dims[i] = x
        return tuple(all_dims)

    def _update_custom_op_kwargs(self, source_meta_tensor: MetaTensor) -> Dict:
        new_dims = self.add_missing_dims(
            len(source_meta_tensor.shape), self.custom_op_kwargs["dims"]
        )
        kwargs = deepcopy(self.custom_op_kwargs)
        kwargs.update({"dims": new_dims})
        return kwargs

    def _update_inv_op_kwargs(self, tensor: Tensor) -> Dict[str, Any]:
        new_dims = self.add_missing_dims(
            self._source.batch_dims + len(_shape(tensor)[self.batch_dims :]),
            self.custom_op_kwargs["dims"],
        )
        kwargs = deepcopy(self.custom_op_kwargs)
        kwargs.update({"dims": tuple(np.argsort(new_dims))})
        return kwargs

    def _stack_onto_(
        self,
        key: str,
        list_item: List[COMPATIBLE_TYPES],
        dim: int,
    ) -> TensorDictBase:

        permute_dims = self.custom_op_kwargs["dims"]
        inv_permute_dims = np.argsort(permute_dims)
        new_dim = [i for i, v in enumerate(inv_permute_dims) if v == dim][0]
        inv_permute_dims = [p for p in inv_permute_dims if p != dim]
        inv_permute_dims = np.argsort(np.argsort(inv_permute_dims))

        list_permuted_items = []
        for item in list_item:
            perm = list(inv_permute_dims) + list(
                range(self.batch_dims - 1, item.ndimension())
            )
            list_permuted_items.append(item.permute(*perm))
        self._source._stack_onto_(key, list_permuted_items, new_dim)
        return self


def _make_repr(key, item: MetaTensor, tensordict):
    if item.is_tensordict():
        return f"{key}: {repr(tensordict[key])}"
    return f"{key}: {item.get_repr()}"


def _td_fields(td: TensorDictBase) -> str:
    return indent(
        "\n"
        + ",\n".join(
            sorted([_make_repr(key, item, td) for key, item in td.items_meta()])
        ),
        4 * " ",
    )


def _check_keys(
    list_of_tensordicts: Sequence[TensorDictBase], strict: bool = False
) -> Set[str]:
    keys: Set[str] = set()
    for td in list_of_tensordicts:
        if not len(keys):
            keys = set(td.keys())
        else:
            if not strict:
                keys = keys.intersection(set(td.keys()))
            else:
                if len(set(td.keys()).difference(keys)) or len(set(td.keys())) != len(
                    keys
                ):
                    raise KeyError(
                        f"got keys {keys} and {set(td.keys())} which are "
                        f"incompatible"
                    )
    return keys


_accepted_classes = (
    Tensor,
    MemmapTensor,
    TensorDictBase,
)
if _has_torchrec:
    _accepted_classes = _accepted_classes + (KeyedJaggedTensor,)


def _expand_to_match_shape(parent_batch_size, tensor, self_batch_dims, self_device):
    if hasattr(tensor, "dtype"):
        return torch.zeros(
            *parent_batch_size,
            *_shape(tensor)[self_batch_dims:],
            dtype=tensor.dtype,
            device=self_device,
        )
    else:
        # tensordict
        out = TensorDict(
            {},
            [*parent_batch_size, *_shape(tensor)[self_batch_dims:]],
            device=self_device,
        )
        return out


def make_tensordict(
    input_dict: Optional[dict] = None,
    batch_size: Optional[Union[Sequence[int], torch.Size, int]] = None,
    device: Optional[DEVICE_TYPING] = None,
    **kwargs,  # source
) -> TensorDict:
    """Returns a TensorDict created from the keyword arguments.

    If batch_size is not specified, returns the maximum batch size possible

    Args:
        input_dict (dictionary, optional): a dictionary to use as a data source
            (nested keys compatible).
        **kwargs (TensorDict or torch.Tensor): keyword arguments as data source
            (incompatible with nested keys).
        batch_size (iterable of int, optional): a batch size for the tensordict.
        device (torch.device or compatible type, optional): a device for the TensorDict.

    """
    if input_dict:
        kwargs.update(input_dict)
    if batch_size is None:
        batch_size = _find_max_batch_size(kwargs)
    # _run_checks=False breaks because a tensor may have the same batch-size as the tensordict
    return TensorDict(
        kwargs,
        batch_size=batch_size,
        device=device,
    )  # _run_checks=False)


def _find_max_batch_size(source: Union[TensorDictBase, dict]) -> list[int]:
    tensor_data = list(source.values())
    batch_size = []
    if not tensor_data:  # when source is empty
        return batch_size
    curr_dim = 0
    while True:
        if tensor_data[0].dim() > curr_dim:
            curr_dim_size = tensor_data[0].size(curr_dim)
        else:
            return batch_size
        for tensor in tensor_data[1:]:
            if tensor.dim() <= curr_dim or tensor.size(curr_dim) != curr_dim_size:
                return batch_size
        batch_size.append(curr_dim_size)
        curr_dim += 1


def _iter_items_lazystack(tensordict):
    for key in tensordict.valid_keys:
        try:
            yield key, tensordict.get(key)
        except KeyError:
            tensordict._update_valid_keys()
            continue


def _clone_value(value, recurse):
    if recurse:
        return value.clone()
    elif isinstance(value, TensorDictBase):
        return value.clone(recurse=False)
    else:
        return value
