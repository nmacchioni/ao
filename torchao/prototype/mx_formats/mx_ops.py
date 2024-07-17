# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""
This file defines the ops needed for our tensor subclass implementation
of `MXTensor` to work naturally in PyTorch programs.  For example, if
the modeling code is written as

  x_mx = MXTensor.to_mx(x, torch.float8_e4m3fn)
  w_mx = MXTensor.to_mx(w, torch.float8_e4m3fn)
  y = F.linear(x_mx, w_mx)

then the ops in this file are used under the hood to properly route
the underlying data fields to the MX matmul.
"""

from typing import Any, Dict

import torch
from torch.utils._pytree import tree_map

from torchao.prototype.mx_formats.constants import DTYPE_FP4_E2M1, DTYPE_FP4_E3M0
from torchao.prototype.mx_formats.mx_tensor import (  # noqa: E501
    MXTensor,
    tensor_size_hp_to_fp4x2,
)

aten = torch.ops.aten

MX_OPS_TABLE: Dict[Any, Any] = {}


def implements(aten_ops):
    """Register aten ops to the mx op table"""

    def decorator(func):
        for op in aten_ops:
            MX_OPS_TABLE[op] = func
        return func

    return decorator


@implements([aten.detach.default])
def mx_desugar_op(aten_op, args, kwargs=None):
    old = args[0]
    new_data = aten_op(old._data, *args[1:], **kwargs)
    new = MXTensor(
        old._scale_e8m0,
        new_data,
        old._elem_dtype,
        old._block_size,
        old._orig_dtype,
    )
    return new


@implements([aten.mm.default, aten.matmul.default])
def mx_mm(aten_op, args, kwargs=None):
    a = args[0]
    b = args[1]
    assert isinstance(a, MXTensor) and isinstance(b, MXTensor)
    a_hp = a.to_dtype(a._orig_dtype)
    b_hp = b.to_dtype(b._orig_dtype)
    res = aten_op(a_hp, b_hp)
    return res


@implements([aten.addmm.default])
def mx_addmm(aten_op, args, kwargs=None):
    a = args[0]
    b = args[1]
    c = args[2]
    assert isinstance(b, MXTensor) and isinstance(c, MXTensor)
    b_hp = b.to_dtype(b._orig_dtype)
    c_hp = c.to_dtype(c._orig_dtype)
    res = aten_op(a, b_hp, c_hp)
    return res


@implements([aten.t.default])
def mx_t(aten_op, args, kwargs=None):
    # For now, only transpose(input, 0, 1) is supported.
    old = args[0]
    new = MXTensor(
        old._scale_e8m0,
        old._data.t(),
        old._elem_dtype,
        old._block_size,
        old._orig_dtype,
    )
    return new


@implements([aten.sum.dim_IntList])
def mx_cast_up_op(aten_op, args, kwargs=None):
    """Be careful with this function, this is a "fallback" op that
    casts the output of the op to the original precision. And performs the op.

    We currently need this to support the backward for admmm bias.
    "addmm" -> out
    "hp_gradBias" <-"sum" <- "identity" <- gradOut <- "hp_gradOut"
    """

    def unwrap(x):
        if isinstance(x, MXTensor):
            return x.to_dtype(x._orig_dtype)
        return x

    new_args = tree_map(unwrap, args)
    new_kwargs = tree_map(unwrap, kwargs)
    return aten_op(*new_args, **new_kwargs)


@implements([aten.view.default])
def mx_view_op(aten_op, args, kwargs=None):
    data = args[0]._data
    new_size = args[1]
    if args[0]._elem_dtype == DTYPE_FP4_E2M1 or args[0]._elem_dtype == DTYPE_FP4_E3M0:
        # special case fp4 as we pack two elements per byte
        new_size = tensor_size_hp_to_fp4x2(new_size, data.is_contiguous())
    new_data = aten_op(data, new_size, *args[2:], **kwargs)
    return MXTensor(
        args[0]._scale_e8m0,
        new_data,
        args[0]._elem_dtype,
        args[0]._block_size,
        args[0]._orig_dtype,
    )


@implements([aten._to_copy.default])
def autocast_to_copy(aten_op, args, kwargs=None):
    """This gets called when running matmul under autocast
    when the input is a MXTensor, presenting as a fp32
    tensor.
    """
    assert isinstance(args[0], MXTensor)
    # print('before', args[0], args[0].dtype, args[0]._orig_dtype)
    assert (
        len(kwargs) == 1 and "dtype" in kwargs
    ), "Only support dtype kwarg for autocast"
    assert kwargs["dtype"] in {
        torch.float16,
        torch.bfloat16,
    }, "Only support floating point conversion for autocast w/ MXTensor"
    res = MXTensor(
        args[0]._scale_e8m0,
        args[0]._data,
        args[0]._elem_dtype,
        args[0]._block_size,
        kwargs["dtype"],
    )
    # print('after', res, res.dtype, res._orig_dtype)
    return res
