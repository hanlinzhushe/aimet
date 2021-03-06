# /usr/bin/env python3.5
# -*- mode: python -*-
# =============================================================================
#  @@-COPYRIGHT-START-@@
#
#  Copyright (c) 2018-2020, Qualcomm Innovation Center, Inc. All rights reserved.
#
#  Redistribution and use in source and binary forms, with or without
#  modification, are permitted provided that the following conditions are met:
#
#  1. Redistributions of source code must retain the above copyright notice,
#     this list of conditions and the following disclaimer.
#
#  2. Redistributions in binary form must reproduce the above copyright notice,
#     this list of conditions and the following disclaimer in the documentation
#     and/or other materials provided with the distribution.
#
#  3. Neither the name of the copyright holder nor the names of its contributors
#     may be used to endorse or promote products derived from this software
#     without specific prior written permission.
#
#  THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
#  AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
#  IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
#  ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
#  LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
#  CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
#  SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
#  INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
#  CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
#  ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
#  POSSIBILITY OF SUCH DAMAGE.
#
#  SPDX-License-Identifier: BSD-3-Clause
#
#  @@-COPYRIGHT-END-@@
# =============================================================================
""" Custom PyTorch Op for quantizing weights and activations """

import abc
from enum import Enum
from typing import Union

from torch import nn
from aimet_common.utils import AimetLogger
from aimet_common.defs import QuantScheme
from aimet_torch.tensor_quantizer import PostTrainingTensorQuantizer

import libpymo


MAP_ROUND_MODE_TO_PYMO = {'nearest':     libpymo.RoundingMode.ROUND_NEAREST,
                          'stochastic':  libpymo.RoundingMode.ROUND_STOCHASTIC}

MAP_QUANT_SCHEME_TO_PYMO = {QuantScheme.post_training_tf_enhanced: libpymo.QuantizationMode.QUANTIZATION_TF_ENHANCED,
                            QuantScheme.post_training_tf: libpymo.QuantizationMode.QUANTIZATION_TF}


_logger = AimetLogger.get_area_logger(AimetLogger.LogAreas.Quant)


class QcQuantizeOpMode(Enum):
    """
    Mode for the Quantization Ops
    """
    PASSTHROUGH = 1
    ANALYSIS = 2
    ACTIVE = 3


def module_has_weights(module):
    """
    Check if the module has a parameter called "weight"
    :param module: Module
    :return: True, if module has a parameter called "weight", False otherwise
    """
    for name, _ in module.named_parameters():
        if name == "weight":
            return True

    return False


def tensor_quantizer_factory(bitwidth: int, round_mode: str, quant_scheme: Union[QuantScheme, libpymo.QuantizationMode],
                             use_symmetric_encodings: bool, enabled_by_default: bool):
    """
    Instantiates TensorQuantizer depending on the quant_scheme
    :param bitwidth: Quantization bitwidth
    :param round_mode: Rounding mode (e.g. Nearest)
    :param quant_scheme: Quantization scheme (e.g. Range Learning)
    :param use_symmetric_encodings: True if symmetric encoding is used.  False otherwise.
    :param enabled_by_default: True if quantization of tensor is enabled.  False otherwise.
    :return: An instance of PostTrainingTensorQuantizer
    """
    assert quant_scheme in [libpymo.QuantizationMode.QUANTIZATION_TF_ENHANCED, libpymo.QuantizationMode.QUANTIZATION_TF]

    tensor_quantizer = PostTrainingTensorQuantizer(bitwidth, round_mode, quant_scheme, use_symmetric_encodings,
                                                   enabled_by_default)
    return tensor_quantizer


class QcQuantizeStandAloneBase(nn.Module):
    """
    Base class for the quantization custom ops
    """

    def __init__(self, activation_bw, round_mode, quant_scheme, is_symmetric):
        """
        Constructor
        :param activation_bw: Quantization bitwidth for activations
        :param round_mode: Rounding mode (e.g. Nearest)
        :param quant_scheme: Quantization scheme (e.g. TF Enhanced)
        :param is_symmetric: Symmetric or asymmetric quantization
        """
        super(QcQuantizeStandAloneBase, self).__init__()
        self.output_quantizer = tensor_quantizer_factory(activation_bw, round_mode,
                                                         quant_scheme,
                                                         is_symmetric,
                                                         enabled_by_default=True)
        self._mode = QcQuantizeOpMode.PASSTHROUGH

    @abc.abstractmethod
    def forward(self, *inputs):
        """
        Forward-pass routine. This quantizes the weights before delegating to the wrapped module and
        then quantizes the output before returning the same
        :param inputs: Inputs passed to the module in the forward pass
        :return: Quantized output from the wrapped module
        """

    def set_output_bw(self, output_bw: int):
        """
        Sets (overrides) the output bitwidth for a particular layer
        :param output_bw: Bitwidth from (4-32)
        :return: None
        """
        self.output_quantizer.bitwidth = output_bw

    def set_mode(self, mode):
        """
        Sets a working mode for the custom op
        :param mode:
        :return:
        """
        self._mode = mode

    def _quantize_activation(self, tensor_quantizer, tensors_to_quantize):
        """
        Forward-pass routine. This quantizes the weights before delegating to the wrapped module and
        then quantizes the output before returning the same
        :param tensor_quantizer: Tensor quantizer to use for updating stats or quantizing
        :param tensors_to_quantize: Inputs passed to the module in the forward pass
        :return: Quantized output from the wrapped module
        """

        outputs = []
        for input_tensor in tensors_to_quantize:

            if self._mode is QcQuantizeOpMode.ANALYSIS:

                tensor_quantizer.update_encoding_stats(input_tensor)
                output = input_tensor

            elif self._mode is QcQuantizeOpMode.ACTIVE:
                # if we are not in training, then only nearest rounding should be used
                # else we should use whatever the user desires (i.e.. stochastic rounding is a valid option)
                if self.training:
                    round_mode = tensor_quantizer.round_mode
                else:
                    round_mode = libpymo.RoundingMode.ROUND_NEAREST
                output = tensor_quantizer.quantize_dequantize(input_tensor, round_mode)

            else:
                output = input_tensor

            outputs.append(output)

        # Flatten if there is only one output - which is by far the most common case
        if len(outputs) == 1:
            outputs = outputs[0]

        return outputs


class QcQuantizeWrapper(nn.Module):
    """
    Base class for the quantization custom ops
    """

    def __init__(self, module_to_wrap: nn.Module, weight_bw: int, activation_bw: int, round_mode, quant_scheme,
                 is_output_quantized=True, is_symmetric=False):
        """
        Constructor
        :param module_to_wrap: Module that will be wrapped with this custom op
        :param weight_bw: Quantization bitwidth for weights
        :param activation_bw: Quantization bitwidth for activations
        :param round_mode: Rounding mode (e.g. Nearest)
        :param quant_scheme: Quantization scheme (e.g. TF Enhanced)
        :param is_output_quantized: True if output tensor quantizer is enabled.  False otherwise.
        :param is_symmetric: True if symmetric encoding is used.  False otherwise.
        """
        super(QcQuantizeWrapper, self).__init__()
        self.output_quantizer = tensor_quantizer_factory(activation_bw, round_mode,
                                                         quant_scheme,
                                                         is_symmetric,
                                                         enabled_by_default=is_output_quantized)
        self._mode = QcQuantizeOpMode.PASSTHROUGH
        self._module_to_wrap = module_to_wrap
        # Using a _is_output_quantized variable instead of directly setting enabled_by_default for QcQuantizeBase since
        # QcQuantizeStandalone shares the same output TensorQuantizer, so we always enable that by default.
        self._is_output_quantized = is_output_quantized

        # Create quantizer for each parameter and compute encodings
        self.param_quantizers = {}
        for name, _ in module_to_wrap.named_parameters():
            _logger.debug("Adding quantizer for parameter: %s", name)
            self.param_quantizers[name] = tensor_quantizer_factory(weight_bw, round_mode,
                                                                   quant_scheme,
                                                                   is_symmetric,
                                                                   enabled_by_default=True)

        # Create quantizer for layer input
        self.input_quantizer = tensor_quantizer_factory(activation_bw, round_mode,
                                                        quant_scheme,
                                                        is_symmetric,
                                                        enabled_by_default=False)

    @abc.abstractmethod
    def forward(self, *inputs):
        """
        Forward-pass routine. This quantizes the weights before delegating to the wrapped module and
        then quantizes the output before returning the same
        :param inputs: Inputs passed to the module in the forward pass
        :return: Quantized output from the wrapped module
        """

    def set_output_bw(self, output_bw: int):
        """
        Sets (overrides) the output bitwidth for a particular layer
        :param output_bw: Bitwidth from (4-32)
        :return: None
        """
        self.output_quantizer.bitwidth = output_bw

    def set_mode(self, mode):
        """
        Sets a working mode for the custom op
        :param mode: Mode for the Quantization Ops. Can be PASSTHROUGH, ANALYSIS or ACTIVE
        """
        self._mode = mode


class QcPostTrainingWrapper(QcQuantizeWrapper):
    """ A custom PyTorch module that derives from QcQuantizeWrapper and quantizes modules """

    def __init__(self, module_to_wrap: nn.Module, weight_bw: int, activation_bw: int, round_mode, quant_scheme,
                 is_output_quantized=True, is_symmetric=False):
        """
        Constructor
        :param module_to_wrap: Module that will be wrapped with this custom op
        :param weight_bw: Quantization bitwidth for weights
        :param activation_bw: Quantization bitwidth for activations
        :param round_mode: Rounding mode (e.g. Nearest)
        :param quant_scheme: Quantization scheme (e.g. TF Enhanced)
        :param is_output_quantized: True if output tensor quantizer is enabled.  False otherwise.
        :param is_symmetric: True if symmetric encoding is used.  False otherwise.
        """
        # Translate round mode and quant scheme into pymo types prior to initializing super()
        round_mode = MAP_ROUND_MODE_TO_PYMO[round_mode]
        quant_scheme = MAP_QUANT_SCHEME_TO_PYMO[quant_scheme]

        super(QcPostTrainingWrapper, self).__init__(module_to_wrap, weight_bw, activation_bw, round_mode, quant_scheme,
                                                    is_output_quantized, is_symmetric)

    def forward(self, *inputs):
        """
        Forward-pass routine. This quantizes the weights before delegating to the wrapped module and
        then quantizes the output before returning the same
        :param inputs: Inputs passed to the module in the forward pass
        :return: Quantized output from the wrapped module
        """

        # Quantize the inputs
        quantized_inputs = self._quantize_activation(self.input_quantizer, inputs)
        if not isinstance(quantized_inputs, list):
            quantized_inputs = [quantized_inputs]

        # Quantize the parameters
        shadow_params = self._quantize_dequantize_params()

        # Call the forward of the wrapped module
        wrapped_output = self._module_to_wrap(*quantized_inputs)

        self._restore_shadow_params(shadow_params)

        # Quantize the outputs
        if not self._is_output_quantized:
            output = wrapped_output
        else:
            if not isinstance(wrapped_output, list):
                wrapped_output = [wrapped_output]
            output = self._quantize_activation(self.output_quantizer, wrapped_output)

        return output

    def _restore_shadow_params(self, shadow_params):

        # Restore the parameters
        for name, param in self._module_to_wrap.named_parameters():
            param.data.zero_()
            param.data.add_(shadow_params[name].data)

    def _quantize_dequantize_params(self):
        """
        Quantizes and dequantizes a parameter
        """

        shadow_params = {}

        # Quantize the parameters, if present
        for name, param in self._module_to_wrap.named_parameters():

            # Store current weight for use later on
            shadow_params[name] = param.detach().clone()

            param_quantizer = self.param_quantizers[name]

            # If we are in training mode with quant-sim nodes, then we want to calculate encodings for the parameters
            # in every pass
            if self._module_to_wrap.training or param_quantizer.encoding is None:
                param_quantizer.reset_encoding_stats()
                param_quantizer.update_encoding_stats(param.data)
                param_quantizer.compute_encoding()

            # if we are not in training, then only nearest rounding should be used
            # else we should use whatever the user desires (i.e.. stochastic rounding is a valid option)
            if self.training:
                round_mode = param_quantizer.round_mode
            else:
                round_mode = libpymo.RoundingMode.ROUND_NEAREST
            param.data = param_quantizer.quantize_dequantize(param.data, round_mode)

        return shadow_params

    def compute_weight_encodings(self):
        """
        Compute quantized model weight encoding.
        :return: weight_encoding value (libpymo.TfEncoding type)
        """

        if 'weight' in self.param_quantizers:
            return self.param_quantizers['weight'].encoding

        return None

    def compute_encoding(self):
        """
        Compute the quantization encoding for this layer
        :return: None
        """
        self.input_quantizer.compute_encoding()
        self.output_quantizer.compute_encoding()

    def _quantize_activation(self, tensor_quantizer, tensors_to_quantize):
        """
        Forward-pass routine. This quantizes the weights before delegating to the wrapped module and
        then quantizes the output before returning the same
        :param tensor_quantizer: Tensor quantizer to use for updating stats or quantizing
        :param tensors_to_quantize: Inputs passed to the module in the forward pass
        :return: Quantized output from the wrapped module
        """

        outputs = []
        for input_tensor in tensors_to_quantize:

            if self._mode is QcQuantizeOpMode.ANALYSIS:

                tensor_quantizer.update_encoding_stats(input_tensor)
                output = input_tensor

            elif self._mode is QcQuantizeOpMode.ACTIVE:
                # if we are not in training, then only nearest rounding should be used
                # else we should use whatever the user desires (i.e.. stochastic rounding is a valid option)
                if self.training:
                    round_mode = tensor_quantizer.round_mode
                else:
                    round_mode = libpymo.RoundingMode.ROUND_NEAREST
                output = tensor_quantizer.quantize_dequantize(input_tensor, round_mode)

            else:
                output = input_tensor

            outputs.append(output)

        # Flatten if there is only one output - which is by far the most common case
        if len(outputs) == 1:
            outputs = outputs[0]

        return outputs


class QcQuantizeStandalone(QcQuantizeStandAloneBase):
    """ A custom PyTorch module that derives from QcQuantizeStandAloneBase and quantizes inputs """

    def forward(self, *inputs):
        """
        Forward-pass routine. This quantizes the weights before delegating to the wrapped module and
        then quantizes the output before returning the same
        :param inputs: Inputs passed to the module in the forward pass
        :return: Quantized output from the wrapped module
        """

        output = self._quantize_activation(self.output_quantizer, list(inputs))

        return output

    def compute_encoding(self):
        """
        Compute the quantization encoding for this op
        :return: None
        """
        self.output_quantizer.compute_encoding()
