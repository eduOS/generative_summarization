# Copyright 2016 The TensorFlow Authors. All Rights Reserved.
# Modifications Copyright 2017 Abigail See
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

"""This file defines the decoder"""
from __future__ import unicode_literals, print_function
from __future__ import absolute_import
from __future__ import division


import tensorflow as tf
from tensorflow.python.ops import variable_scope
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import nn_ops
from tensorflow.python.ops import math_ops
from utils import linear
from utils import selective_fn

# Note: this function is based on tf.contrib.legacy_seq2seq_attention_decoder,
# which is now outdated.
# In the future, it would make more sense to write variants on the
# attention mechanism using the new seq2seq library for tensorflow 1.0:
# https://www.tensorflow.org/api_guides/python/contrib.seq2seq#Attention


def attention_decoder(decoder_inputs, initial_state, encoder_states, enc_padding_mask,
                      cell, initial_state_attention=False,
                      use_coverage=False, prev_coverage=None):
    copy_states = selective_fn(encoder_states, initial_state)
    # copy_states = tf.squeeze(copy_states, [3])

    # can this be applied to beam repetitive batch?
    with variable_scope.variable_scope("attention_decoder"):
        # if this line fails, it's because the batch size isn't defined
        batch_size = array_ops.shape(encoder_states)[0]
        # if this line fails, it's because the attention length isn't defined
        attn_size = encoder_states.get_shape()[2].value

        # Reshape encoder_states (need to insert a dim)
        # now is shape (batch_size, attn_len, 1, attn_size)
        # the length is one
        encoder_states = tf.expand_dims(encoder_states, axis=2)
        copy_states = tf.expand_dims(copy_states, axis=2)

        # To calculate attention, we calculate
        #   v^T tanh(W_h h_i + W_s s_t + b_attn)
        # where h_i is an encoder state, and s_t a decoder state.
        # attn_vec_size is the length of the vectors v, b_attn, (W_h h_i) and
        # (W_s s_t).
        # We set it to be equal to the size of the encoder states.
        attention_vec_size = attn_size

        # Get the weight matrix W_h and apply it to each encoder state to get
        # (W_h h_i), the encoder features
        # Input:   batch,         in_hight,     in_width,    in_channels
        #          batch_size,    attn_len,     1,           attn_size
        # Filter:  filter_height, filter_width, in_channels, out_channels
        #          1,             1,            attn_size,   attention_vec_size
        # Output:  batch,         in_hight,     in_width,    out_channels
        #          batch_size,    attn_len,     1,           attention_vec_size
        W_h = variable_scope.get_variable("W_h", [1, 1, attn_size, attention_vec_size])
        # shape (batch_size,attn_length,1,attention_vec_size)
        # why add transfer this to the same shape of the input?
        encoder_features = nn_ops.conv2d(encoder_states, W_h, [1, 1, 1, 1], "SAME")
        copy_features = nn_ops.conv2d(copy_states, W_h, [1, 1, 1, 1], "SAME")
        # this is just to create a tensor of this shape

        # Get the weight vectors v and w_c (w_c is for coverage)
        v = variable_scope.get_variable("v", [attention_vec_size])
        vk = variable_scope.get_variable("vk", [attention_vec_size])

        if use_coverage:
            with variable_scope.variable_scope("coverage"):
                w_c = variable_scope.get_variable("w_c", [1, 1, 1, attention_vec_size])

        if prev_coverage is not None:  # for beam search mode with coverage
            # reshape from (batch_size, attn_length) to (batch_size, attn_len,
            # 1, 1)
            prev_coverage = tf.expand_dims(tf.expand_dims(prev_coverage, 2), 3)

        def copy_attention(decoder_state, coverage=None):
            """Calculate the context vector and attention distribution from the
            decoder state.

            Args:
              decoder_state: state of the decoder
              coverage: Optional. Previous timestep's coverage vector, shape
              (batch_size, attn_len, 1, 1).

            Returns:
              context_vector: weighted sum of encoder_states
              attn_dist: attention distribution
              coverage: new coverage vector. shape (batch_size, attn_len, 1, 1)
            """
            with variable_scope.variable_scope("Attention"):
                # Pass the decoder state through a linear layer (this is W_s s_t
                # + b_attn in the paper) shape (batch_size, attention_vec_size)
                decoder_features = linear(decoder_state, attention_vec_size, True)
                # reshape to (batch_size, 1, 1, attention_vec_size)
                decoder_features = tf.expand_dims(tf.expand_dims(decoder_features, 1), 1)
                # why not reshape?

                def masked_attention(e):
                    """Take softmax of e then apply enc_padding_mask and re-normalize"""
                    attn_dist = nn_ops.softmax(e)  # take softmax. shape (batch_size, attn_length)
                    attn_dist *= enc_padding_mask  # apply mask
                    masked_sums = tf.reduce_sum(attn_dist, axis=1)  # shape (batch_size)
                    return attn_dist / tf.reshape(masked_sums, [-1, 1])  # re-normalize

                if use_coverage and coverage is not None:
                    # non-first step of coverage
                    # Multiply coverage vector by w_c to get coverage_features.
                    # c has shape (batch_size, attn_length, 1,
                    # attention_vec_size)
                    coverage_features = nn_ops.conv2d(coverage, w_c, [1, 1, 1, 1], "SAME")

                    # Calculate v^T tanh(W_h h_i + W_s s_t + w_c c_i^t + b_attn)
                    # shape (batch_size,attn_length)
                    e = math_ops.reduce_sum(
                        v * math_ops.tanh(
                            encoder_features +  # encoder features are the convolution of the encoder states
                            decoder_features +  # decoder features are just the projection of the decode state
                            coverage_features), [2, 3])

                    # (batch_size, attn_length, 1, attention_vec_size)
                    # (batch_size, 1,           1, attention_vec_size)
                    # (batch_size, attn_length, 1, attention_vec_size)

                    # Take softmax of e to get the attention distribution
                    # shape (batch_size, attn_length)
                    attn_dist = masked_attention(e)

                    # Update coverage vector, the initial coverage is zero
                    coverage += array_ops.reshape(attn_dist, [batch_size, -1, 1, 1])
                else:
                    # Calculate v^T tanh(W_h h_i + W_s s_t + b_attn)
                    e = math_ops.reduce_sum(
                        v * math_ops.tanh(encoder_features + decoder_features),
                        [2, 3])  # calculate e

                    k = math_ops.reduce_sum(
                        vk * math_ops.tanh(copy_features + decoder_features),
                        [2, 3])

                    # Take softmax of e to get the attention distribution
                    # shape (batch_size, attn_length)
                    attn_dist = masked_attention(e)
                    copy_dist = masked_attention(k)

                    if use_coverage:  # first step of training
                        coverage = tf.expand_dims(tf.expand_dims(attn_dist, 2), 2)
                        # shape (batch_size, attn_length, 1, 1)
                        # initialize coverage

                # Calculate the context vector from attn_dist and encoder_states
                context_vector = math_ops.reduce_sum(
                    array_ops.reshape(attn_dist, [batch_size, -1, 1, 1]) * encoder_states, [1, 2])
                copy_vector = math_ops.reduce_sum(
                    array_ops.reshape(copy_dist, [batch_size, -1, 1, 1]) * copy_states, [1, 2])
                # shape (batch_size, attn_size).
                context_vector = array_ops.reshape(context_vector, [-1, attn_size])
                copy_vector = array_ops.reshape(copy_vector, [-1, attn_size])

            return context_vector, copy_vector, copy_dist, coverage

        outputs = []
        copy_dists = []
        p_gens = []
        state = initial_state
        coverage = prev_coverage
        # initialize coverage to None or whatever was passed in
        context_vector = array_ops.zeros([batch_size, attn_size])
        # Ensure the second shape of attention vectors is set.
        context_vector.set_shape([None, attn_size])
        if initial_state_attention:  # true in decode mode
            # Re-calculate the context vector from the previous step so that we
            # can pass it through a linear layer with this step's input to get a
            # modified version of the input
            # in decode mode, this is what updates the coverage vector
            context_vector, _, _, coverage = copy_attention(initial_state, coverage)
        for i, inp in enumerate(decoder_inputs):
            # when should this terminate due to beam size
            if i > 0:
                variable_scope.get_variable_scope().reuse_variables()

            # Merge input and previous attentions into one vector x of the same
            # size as inp
            input_size = inp.get_shape().with_rank(2)[1]
            if input_size.value is None:
                raise ValueError("Could not infer input size from input: %s" % inp.name)
            x = linear([inp] + [context_vector], input_size, True)
            # is this the same in either mode?
            # only for the training, while decoding is is the beam search

            # Run the decoder RNN cell. cell_output = decoder state
            cell_output, state = cell(x, state)
            # state is the h_i^{d}. e_{ti}

            # Run the attention mechanism.
            if i == 0 and initial_state_attention:  # always true in decode mode
                # you need this because you've already run the initial
                # attention(...) call
                with variable_scope.variable_scope(variable_scope.get_variable_scope(), reuse=True):
                    context_vector, copy_vector, copy_dist, _ = copy_attention(state, coverage)
                    # don't allow coverage to update
            else:
                context_vector, copy_vector, copy_dist, coverage = copy_attention(state, coverage)
            copy_dists.append(copy_dist)

            # Calculate p_gen
            with tf.variable_scope('calculate_pgen'):
                p_gen = linear([copy_vector, state.c, state.h, x], 1, True)
                # a scalar
                p_gen = tf.sigmoid(p_gen)
                p_gens.append(p_gen)

            # Concatenate the cell_output (= decoder state) and the context
            # vector, and pass them through a linear layer
            # This is V[s_t, h*_t] + b in the paper
            with variable_scope.variable_scope("AttnOutputProjection"):
                output = linear([cell_output] + [context_vector], cell.output_size, True)
            outputs.append(output)

        # If using coverage, reshape it
        if coverage is not None:
            coverage = array_ops.reshape(coverage, [batch_size, -1])

        return outputs, state, copy_dists, p_gens, coverage
