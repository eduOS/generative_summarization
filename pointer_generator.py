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

"""This file contains code to build and run the tensorflow graph for the
sequence-to-sequence model"""
from __future__ import unicode_literals, print_function
from __future__ import absolute_import
from __future__ import division

import os
import sys
import time
import numpy as np
import tensorflow as tf
from attention_decoder import attention_decoder
# from share_function import tableLookup
from tensorflow.contrib.tensorboard.plugins import projector
from six.moves import xrange

FLAGS = tf.app.flags.FLAGS


class PointerGenerator(object):
    """A class to represent a sequence-to-sequence model for text summarization.
    Supports both baseline mode, pointer-generator mode, and coverage"""

    def __init__(self, hps, vocab):
        self.hps = hps
        self._vocab = vocab

    def _add_placeholders(self):
        """Add placeholders to the graph. These are entry points for any input
        data."""
        hps = self.hps
        batch_size = None

        # encoder part
        self.enc_batch = tf.placeholder(tf.int32, [batch_size, None], name='enc_batch')
        self.enc_lens = tf.placeholder(tf.int32, [batch_size], name='enc_lens')
        self.enc_padding_mask = tf.placeholder(tf.float32, [batch_size, None], name='enc_padding_mask')
        if FLAGS.pointer_gen:
            self.enc_batch_extend_vocab = tf.placeholder(tf.int32, [batch_size, None], name='enc_batch_extend_vocab')
            self.max_art_oovs = tf.placeholder(tf.int32, [], name='max_art_oovs')

        # decoder part
        self._dec_batch = tf.placeholder(tf.int32, [batch_size, hps.max_dec_steps], name='dec_batch')
        self._target_batch = tf.placeholder(tf.int32, [batch_size, hps.max_dec_steps], name='target_batch')
        self._padding_mask = tf.placeholder(tf.float32, [batch_size, hps.max_dec_steps], name='padding_mask')
        self.rewards = tf.placeholder(tf.float32, shape=[batch_size, hps.max_dec_steps])
        self.g_predictions = tf.placeholder(tf.int32, shape=[batch_size, hps.max_dec_steps])

        if hps.mode in ["decode", 'gan'] and hps.coverage:
            self.prev_coverage = tf.placeholder(tf.float32, [None, None], name='prev_coverage')
            # so this need not to be reloaded and taken gradient hps

    def _make_feed_dict(self, batch, just_enc=False):
        """Make a feed dictionary mapping parts of the batch to the appropriate
        placeholders.

        Args:
          batch: Batch object
          just_enc: Boolean. If True, only feed the parts needed for the
          encoder.
        """
        feed_dict = {}
        feed_dict[self.enc_batch] = batch.enc_batch
        feed_dict[self.enc_lens] = batch.enc_lens
        feed_dict[self.enc_padding_mask] = batch.enc_padding_mask
        if FLAGS.pointer_gen:
            feed_dict[self.enc_batch_extend_vocab] = batch.enc_batch_extend_vocab
            feed_dict[self.max_art_oovs] = batch.max_art_oovs
            # the unique feature for the pointer gen is the
            # enc_batch_extend_vocab and the max_art_oovs
        if not just_enc:
            feed_dict[self._dec_batch] = batch.dec_batch
            feed_dict[self._target_batch] = batch.target_batch
            feed_dict[self._padding_mask] = batch.padding_mask
        return feed_dict

    def _add_encoder(self, encoder_inputs, seq_len):
        """Add a single-layer bidirectional LSTM encoder to the graph.

        Args:
          encoder_inputs: A tensor of shape [batch_size, <=max_enc_steps,
          emb_size].
          seq_len: Lengths of encoder_inputs (before padding). A tensor of shape
          [batch_size].

        Returns:
          encoder_outputs:
            A tensor of shape [batch_size, <=max_enc_steps, 2*hidden_dim]. It's
            2*hidden_dim because it's the concatenation of the forwards and
            backwards states.
          fw_state, bw_state:
            Each are LSTMStateTuples of shape
            ([batch_size,hidden_dim],[batch_size,hidden_dim])
        """
        with tf.variable_scope('encoder'):
            cell_fw = tf.contrib.rnn.LSTMCell(
                self.hps.hidden_dim, initializer=self.rand_unif_init, state_is_tuple=True)
            cell_bw = tf.contrib.rnn.LSTMCell(
                self.hps.hidden_dim, initializer=self.rand_unif_init, state_is_tuple=True)
            (encoder_outputs, (fw_st, bw_st)) = tf.nn.bidirectional_dynamic_rnn(
                cell_fw, cell_bw, encoder_inputs, dtype=tf.float32, sequence_length=seq_len, swap_memory=True)
            # the sequence length of the encoder_inputs varies depending on the
            # batch, which will make the second dimension of the
            # encoder_outputs different in different batches

            # concatenate the forwards and backwards states
            encoder_outputs = tf.concat(axis=2, values=encoder_outputs)
            # encoder_outputs: [batch_size * beam_size, max_time, output_size*2]
            # fw_st & bw_st: [batch_size * beam_size, num_hidden]
        return encoder_outputs, fw_st, bw_st

    def _reduce_states(self, fw_st, bw_st):
        """Add to the graph a linear layer to reduce the encoder's final FW and
        BW state into a single initial state for the decoder. This is needed
        because the encoder is bidirectional but the decoder is not.

        Args:
          fw_st: LSTMStateTuple with hidden_dim units.
          bw_st: LSTMStateTuple with hidden_dim units.

        Returns:
          state: LSTMStateTuple with hidden_dim units.
        """
        hidden_dim = self.hps.hidden_dim
        with tf.variable_scope('reduce_final_st'):

            # Define weights and biases to reduce the cell and reduce the state
            w_reduce_c = tf.get_variable(
                'w_reduce_c', [hidden_dim * 2, hidden_dim],
                dtype=tf.float32, initializer=self.trunc_norm_init)
            w_reduce_h = tf.get_variable(
                'w_reduce_h', [hidden_dim * 2, hidden_dim],
                dtype=tf.float32, initializer=self.trunc_norm_init)
            bias_reduce_c = tf.get_variable(
                'bias_reduce_c', [hidden_dim],
                dtype=tf.float32, initializer=self.trunc_norm_init)
            bias_reduce_h = tf.get_variable(
                'bias_reduce_h', [hidden_dim],
                dtype=tf.float32, initializer=self.trunc_norm_init)

            # Apply linear layer
            # Concatenation of fw and bw cell
            old_c = tf.concat(axis=1, values=[fw_st.c, bw_st.c])
            # Concatenation of fw and bw state
            old_h = tf.concat(axis=1, values=[fw_st.h, bw_st.h])
            # [batch_size * beam_size, hidden_dim]
            new_c = tf.nn.relu(tf.matmul(old_c, w_reduce_c) + bias_reduce_c)  # Get new cell from old cell
            new_h = tf.nn.relu(tf.matmul(old_h, w_reduce_h) + bias_reduce_h)  # Get new state from old state
            return tf.contrib.rnn.LSTMStateTuple(new_c, new_h)  # Return new cell and state

    def _add_decoder(self, inputs, dec_in_state):
        """Add attention decoder to the graph. In train or eval mode, you call
        this once to get output on ALL steps. In decode (beam search) mode, you
        call this once for EACH decoder step.

        Args:
          inputs: inputs to the decoder (word embeddings). A list of tensors
          shape (batch_size, emb_dim)

        Returns:
          outputs: List of tensors; the outputs of the decoder
          out_state: The final state of the decoder
          attn_dists: A list of tensors; the attention distributions
          p_gens: A list of scalar tensors; the generation probabilities
          coverage: A tensor, the current coverage vector
        """
        cell = tf.contrib.rnn.LSTMCell(
            self.hps.hidden_dim,
            state_is_tuple=True,
            initializer=self.rand_unif_init)

        # In decode mode, we run attention_decoder one step at a time and so
        # need to pass in the previous step's coverage vector each time
        # a placeholder, why not a variable?
        prev_coverage = self.prev_coverage if self.hps.coverage and self.hps.mode in ["gan", "decode"] else None
        # coverage is for decoding in beam_search and gan training

        outputs, out_state, attn_dists, p_gens, coverage = attention_decoder(
            inputs, dec_in_state, self.enc_states, self.enc_padding_mask, cell,
            initial_state_attention=(self.hps.mode in ["decode", 'gan']),
            pointer_gen=self.hps.pointer_gen, use_coverage=self.hps.coverage,
            prev_coverage=prev_coverage)

        return outputs, out_state, attn_dists, p_gens, coverage

    def _calc_final_dist(self, p_gens, vocab_dists, attn_dists):
        # this is the core function
        """Calculate the final distribution, for the pointer-generator model

        Args:
          vocab_dists: The vocabulary distributions. List length max_dec_steps
          of (batch_size, vsize) arrays. The words are in the order they appear
          in the vocabulary file.
          attn_dists: The attention distributions. List length max_dec_steps of
          (batch_size, attn_len) arrays

        Returns:
          final_dists: The final distributions. List length max-dec_steps of
          (batch_size, extended_vsize) arrays.
        """
        batch_size = tf.shape(vocab_dists[0])[0]
        with tf.variable_scope('final_distribution'):
            # Multiply vocab dists by p_gen and attention dists by (1-p_gen)
            # these three variable is confusing: vocab_dists, p_gens and
            # attn_dists
            vocab_dists = [
                p_gen * dist
                for (p_gen, dist) in zip(p_gens, vocab_dists)]
            # vocab_dists [max_dec_steps * (batch_size, vsize)]
            attn_dists = [
                (1 - p_gen) * dist
                for (p_gen, dist) in zip(p_gens, attn_dists)]
            # vocab_dists [max_dec_steps * (batch_size, attn_len)]

            # Concatenate some zeros to each vocabulary dist, to hold the
            # probabilities for in-article OOV words
            # the maximum (over the batch) size of the extended vocabulary
            extended_vsize = self._vocab.size() + self.max_art_oovs
            extra_zeros = tf.zeros((batch_size, self.max_art_oovs))
            vocab_dists_extended = [
                tf.concat(axis=1, values=[dist, extra_zeros])
                for dist in vocab_dists]
            # list length max_dec_steps of shape (batch_size, extended_vsize)

            # Project the values in the attention distributions onto the
            # appropriate entries in the final distributions
            # This means that if a_i = 0.1 and the ith encoder word is w, and w
            # has index 500 in the vocabulary, then we add 0.1 onto the 500th
            # entry of the final distribution
            # This is done for each decoder timestep.
            # This is fiddly; we use tf.scatter_nd to do the projection
            batch_nums = tf.range(0, limit=batch_size)
            # shape (batch_size)
            batch_nums = tf.expand_dims(batch_nums, 1)
            # shape (batch_size, 1)
            attn_len = tf.shape(self.enc_batch_extend_vocab)[1]
            # number of states we attend over
            # this is too tedious
            # shape (batch_size, attn_len)
            batch_nums = tf.tile(batch_nums, [1, attn_len])
            indices = tf.stack((batch_nums, self.enc_batch_extend_vocab), axis=2)
            # shape (batch_size, enc_t, 2)
            # what is this enc_batch_extend_vocab?
            shape = [batch_size, extended_vsize]
            attn_dists_projected = [
                tf.scatter_nd(indices, copy_dist, shape)
                for copy_dist in attn_dists]
            # a detailed article should be written about this
            # list length max_dec_steps (batch_size, extended_vsize)

            # Add the vocab distributions and the copy distributions together to
            # get the final distributions
            # final_dists is a list length max_dec_steps; each entry is a tensor
            # shape (batch_size, extended_vsize) giving the final distribution
            # for that decoder timestep
            # Note that for decoder timesteps and examples corresponding to a
            # [PAD] token, this is junk - ignore.
            final_dists = [
                vocab_dist + copy_dist for (vocab_dist, copy_dist) in zip(
                    vocab_dists_extended, attn_dists_projected)]

            # OOV part of vocab is max_art_oov long. Not all the sequences in a
            # batch will have max_art_oov tokens.  That will cause some entries
            # to be 0 in the distribution, which will result in NaN when
            # calulating log_dists Add a very small number to prevent that.

            def add_epsilon(dist, epsilon=sys.float_info.epsilon):
                epsilon_mask = tf.ones_like(dist) * epsilon
                return dist + epsilon_mask

            final_dists = [add_epsilon(dist) for dist in final_dists]

            return final_dists

    def _add_emb_vis(self, embedding_var):
        """Do setup so that we can view word embeddings visualization in
        Tensorboard, as described here:
        https://www.tensorflow.org/get_started/embedding_viz
        Make the vocab metadata file, then make the projector config file
        pointing to it."""
        train_dir = os.path.join(FLAGS.log_root, "train")
        vocab_metadata_path = os.path.join(train_dir, "vocab_metadata.tsv")
        self._vocab.write_metadata(vocab_metadata_path)  # write metadata file
        summary_writer = tf.summary.FileWriter(train_dir)
        config = projector.ProjectorConfig()
        embeddings = config.embeddings.add()
        embeddings.tensor_name = embedding_var.name
        embeddings.metadata_path = vocab_metadata_path
        projector.visualize_embeddings(summary_writer, config)

    def _add_seq2seq(self):
        """Add the whole sequence-to-sequence model to the graph."""
        hps = self.hps

        with tf.variable_scope('seq2seq'):
            # Some initializers
            self.rand_unif_init = tf.random_uniform_initializer(
                -hps.rand_unif_init_mag, hps.rand_unif_init_mag, seed=123)
            self.trunc_norm_init = tf.truncated_normal_initializer(stddev=hps.trunc_norm_init_std)

            with tf.variable_scope('embeddings'):
                self.embeddings = tf.get_variable('embeddings', [self._vocab.size(), hps.emb_dim], dtype=tf.float32, initializer=self.trunc_norm_init)
                emb_enc_inputs = tf.nn.embedding_lookup(self.embeddings, self.enc_batch)
                emb_dec_inputs = [tf.nn.embedding_lookup(self.embeddings, x)
                                  for x in tf.unstack(self._dec_batch, axis=1)]

            # Add the encoder.
            enc_outputs, fw_st, bw_st = self._add_encoder(emb_enc_inputs, self.enc_lens)
            # encoder_outputs: [batch_size * beam_size, max_time, num_hidden*2]
            # fw_st & bw_st: [batch_size * beam_size, num_hidden]
            # those in the encoder should also be updated
            self.enc_states = enc_outputs
            # this is for the decode-one-step process: beam search and rollout
            # this don't need a

            # Our encoder is bidirectional and our decoder is unidirectional so
            # we need to reduce the final encoder hidden state to the right size
            # to be the initial decoder hidden state
            self.dec_in_state = self._reduce_states(fw_st, bw_st)
            # tf.contrib.rnn.LSTMStateTuple(
            # [batch_size * beam_size, hidden_dim], [batch_size * beam_size, hidden_dim])
            # a lstm tuple with each item being: [batch_size * beam_size, hidden_dim]
            # where is the batch size

            self.attn_dists, self.p_gens, self.coverage, vocab_scores, \
                final_dists, self._dec_out_state = self.decode(emb_dec_inputs, self.dec_in_state)

            # Calculate the loss
            with tf.variable_scope('train_loss'):
                if FLAGS.pointer_gen:  # calculate loss from log_dists
                    # Calculate the loss per step This is fiddly; we use
                    # tf.gather_nd to pick out the log probabilities of the
                    # target words will be list length max_dec_steps containing
                    # shape (batch_size)
                    loss_per_step = []
                    batch_nums = tf.range(0, tf.shape(emb_dec_inputs[0])[0])
                    # shape (batch_size)
                    for dec_step, dist in enumerate(final_dists):
                        # The indices of the target words. shape
                        # (batch_size)
                        targets = self._target_batch[:, dec_step]
                        indices = tf.stack((batch_nums, targets), axis=1)
                        # why stack a batch_nums?
                        # shape (batch_size, 2)
                        # shape (batch_size). loss on this step for each
                        # batch
                        gold_probs = tf.gather_nd(dist, indices)
                        losses = -tf.log(gold_probs)
                        # amazing!
                        loss_per_step.append(losses)

                    # Apply padding_mask mask and get loss
                    self._loss = _mask_and_avg(loss_per_step, self._padding_mask)

                else:  # baseline model
                    self._loss = tf.contrib.seq2seq.sequence_loss(
                        tf.stack(vocab_scores, axis=1), self._target_batch,
                        self._padding_mask, average_across_timesteps=True,
                        average_across_batch=True)
                    # set both batch and timesteps as true to compare it with
                    # the loss

                tf.summary.scalar('loss', self._loss)

                # Calculate coverage loss from the attention distributions
                if hps.coverage:
                    with tf.variable_scope('coverage_loss'):
                        self._coverage_loss = _coverage_loss(
                            self.attn_dists, self._padding_mask)
                        tf.summary.scalar(
                            'coverage_loss', self._coverage_loss)
                    self._total_loss = \
                        self._loss + hps.cov_loss_wt * self._coverage_loss
                    tf.summary.scalar('total_loss', self._total_loss)

        # We run decode beam search mode one decoder step at a time
        # log_dists is a singleton list containing shape (batch_size,
        # extended_vsize)
        if len(emb_dec_inputs) == 1:
            # what is dimention of embe_dec_inputs while decoding?
            assert len(final_dists) == 1
            self.final_dists = final_dists[0]
            topk_probs, self._topk_ids = tf.nn.top_k(
                self.final_dists, hps.beam_size * 2)
            self._topk_log_probs = tf.log(topk_probs)
        # note batch_size=beam_size in decode mode

        with tf.variable_scope('gan_loss'):
            g_predictions = tf.nn.embedding_lookup(self.embeddings, self.g_predictions)
            self.g_loss = -tf.reduce_sum(
                tf.reduce_sum(
                    tf.one_hot(tf.to_int32(tf.reshape(self.enc_batch, [-1])),
                               self.hps.emb_dim, 1.0, 0.0) * tf.clip_by_value(
                                   tf.reshape(g_predictions, [-1, self.hps.emb_dim]), 1e-20, 1.0), 1
                ) * tf.reshape(self.rewards, [-1])
            )
            # rewards and g_predictions should be placeholders

            g_opt = self.g_optimizer(self.hps.gen_lr)

            trainable_variables = tf.trainable_variables()
            self.g_grad, _ = tf.clip_by_global_norm(tf.gradients(self.g_loss, trainable_variables), self.hps.gen_max_gradient)
            self.g_updates = g_opt.apply_gradients(zip(self.g_grad, trainable_variables))

    def decode(self, emb_dec_inputs, dec_in_state):
        """
        input:
            emb_dec_inputs, the input of the cell
        to get:
            output log distribution
            new state
        """
        vsize = self._vocab.size()  # size of the vocabulary
        # Add the decoder.
        decoder_outputs, dec_out_state, \
            attn_dists, p_gens, coverage = self._add_decoder(emb_dec_inputs, dec_in_state)

        # Add the output projection to obtain the vocabulary distribution
        with tf.variable_scope('output_projection'):
            w = tf.get_variable(
                'w', [self.hps.hidden_dim, vsize],
                dtype=tf.float32, initializer=self.trunc_norm_init)
            w_t = tf.transpose(w)  # NOQA
            v = tf.get_variable('v', [vsize], dtype=tf.float32, initializer=self.trunc_norm_init)
            vocab_scores = []
            # vocab_scores is the vocabulary distribution before applying
            # softmax. Each entry on the list corresponds to one decoder
            # step
            for i, output in enumerate(decoder_outputs):
                vocab_scores.append(tf.nn.xw_plus_b(output, w, v))
                # apply the linear layer

            # The vocabulary distributions. List length max_dec_steps of
            # (batch_size, vsize) arrays. The words are in the order they
            # appear in the vocabulary file.
            vocab_dists = [tf.nn.softmax(s) for s in vocab_scores]
            # if not FLAGS.pointer_gen:  # calculate loss from log_dists
            #     self.vocab_scores = vocab_scores
            # is the oov included

        # For pointer-generator model, calc final distribution from copy
        # distribution and vocabulary distribution, then take log
        if FLAGS.pointer_gen:
            final_dists = self._calc_final_dist(p_gens, vocab_dists, attn_dists)
            # Take log of final distribution
            # log_dists = [tf.log(dist) for dist in final_dists]
        else:  # just take log of vocab_dists
            final_dists = vocab_dists
            # log_dists = [tf.log(dist) for dist in vocab_dists]
        return attn_dists, p_gens, coverage, vocab_scores, final_dists, dec_out_state

    def _add_train_op(self):
        """Sets self._train_op, the op to run for training."""
        # Take gradients of the trainable variables w.r.t. the loss function to
        # minimize
        loss_to_minimize = \
            self._total_loss if self.hps.coverage else self._loss
        tvars = tf.trainable_variables()
        gradients = tf.gradients(
            loss_to_minimize, tvars,
            aggregation_method=tf.AggregationMethod.EXPERIMENTAL_TREE)

        # Clip the gradients
        with tf.device("/gpu:0"):
            grads, global_norm = tf.clip_by_global_norm(
                gradients, self.hps.gen_max_gradient)

        # Add a summary
        tf.summary.scalar('global_norm', global_norm)

        # Apply adagrad optimizer
        optimizer = tf.train.AdagradOptimizer(
            self.hps.gen_lr, initial_accumulator_value=self.hps.adagrad_init_acc)
        with tf.device("/gpu:0"):
            self._train_op = optimizer.apply_gradients(
                zip(grads, tvars),
                global_step=self.global_step)

    def build_graph(self):
        """Add the placeholders, model, global step, train_op and summaries to
        the graph"""
        tf.logging.info('Building graph...')
        t0 = time.time()
        self._add_placeholders()
        with tf.device("/gpu:0"):
            self._add_seq2seq()
        self.global_step = tf.Variable(0, name='global_step', trainable=False)
        self._add_train_op()
        self._summaries = tf.summary.merge_all()
        t1 = time.time()
        tf.logging.info('Time to build graph: %i seconds', t1 - t0)

    def run_train_step(self, sess, batch):
        """Runs one training iteration. Returns a dictionary containing train
        op, summaries, loss, global_step and (optionally) coverage loss."""
        feed_dict = self._make_feed_dict(batch)
        to_return = {
            'train_op': self._train_op,
            'summaries': self._summaries,
            'loss': self._loss,
            'global_step': self.global_step,
        }
        if self.hps.coverage:
            to_return['coverage_loss'] = self._coverage_loss
        return sess.run(to_return, feed_dict)

    def run_eval_step(self, sess, batch):
        """Runs one evaluation iteration. Returns a dictionary containing
        summaries, loss, global_step and (optionally) coverage loss."""
        feed_dict = self._make_feed_dict(batch)
        to_return = {
            'summaries': self._summaries,
            'loss': self._loss,
            'global_step': self.global_step,
        }
        if self.hps.coverage:
            to_return['coverage_loss'] = self._coverage_loss
        return sess.run(to_return, feed_dict)

    def run_encoder(self, sess, batch):
        """For beam search decoding. Run the encoder on the batch and return the
        encoder states and decoder initial state.

        Args:
          sess: Tensorflow session.
          batch: Batch object that is the same example repeated across the batch
          (for beam search)

        Returns:
          enc_states: The encoder states. A tensor of shape [batch_size,
          <=max_enc_steps, 2*hidden_dim].
          dec_in_state: A LSTMStateTuple of shape
          ([batch_size, hidden_dim],[batch_size, hidden_dim])
        """
        feed_dict = self._make_feed_dict(batch, just_enc=True)
        # feed the batch into the placeholders
        (enc_states, dec_in_state, global_step) = sess.run(
            [
                self.enc_states,
                self.dec_in_state,
                self.global_step
            ],
            feed_dict
        )  # run the encoder
        # enc_states: [batch_size * beam_size, <=max_enc_steps, 2*hidden_dim]
        # dec_in_state: [batch_size * beam_size, ]

        # dec_in_state is LSTMStateTuple shape
        # ([batch_size,hidden_dim],[batch_size,hidden_dim])
        # Given that the batch is a single example repeated, dec_in_state is
        # identical across the batch so we just take the top row.
        # dec_in_state = [tf.contrib.rnn.LSTMStateTuple(
        #     dec_in_state.c[i], dec_in_state.h[i] for i in xrange(len(dec_in_state.h))]
        #     # TODO: should this be changed to shape?
        return enc_states, dec_in_state

    def decode_onestep(self, emb_dec_inputs, dec_in_state):
        """
        function: decode onestep for rollout
        inputs:
            the embedded input
        """
        # attn_dists, p_gens, coverage, vocab_scores, log_probs, new_states
        _, _, _, _, final_dists, new_states = self.decode(emb_dec_inputs, dec_in_state)
        # how can it be fed by a [batch_size * 1 * emb_dim] while decoding?
        output_id = tf.squeeze(tf.cast(tf.multinomial(final_dists[0], 1), tf.int32))
        # next_input = tf.nn.embedding_lookup(self.embeddings, next_token)  # batch x emb_dim
        return output_id, new_states

    def run_decode_onestep(self, sess, enc_batch_extend_vocab, max_art_oovs,
                           latest_tokens, enc_states, enc_padding_mask,
                           dec_init_states, prev_coverage):
        """For beam search decoding. Run the decoder for one step.

        Args:
          sess: Tensorflow session.
          enc_batch_extend_vocab: the encode batch with extended vocabulary
          max_art_oovs: the max article out of vocabulary
          latest_tokens: Tokens to be fed as input into the decoder for this
          timestep
          enc_states: The encoder states.
          dec_init_states: List of beam_size LSTMStateTuples; the decoder states
          from the previous timestep
          prev_coverage: List of np arrays. The coverage vectors from the
          previous timestep. List of None if not using coverage.

        Returns:
          ids: top 2k ids. shape [beam_size, 2*beam_size]
          probs: top 2k log probabilities. shape [beam_size, 2*beam_size]
          new_states: new states of the decoder. a list length beam_size
          containing
            LSTMStateTuples each of shape ([hidden_dim,],[hidden_dim,])
          attn_dists: List length beam_size containing lists length attn_length.
          p_gens: Generation probabilities for this step. A list length
          beam_size. List of None if in baseline mode.
          new_coverage: Coverage vectors for this step. A list of arrays. List
          of None if coverage is not turned on.
        """

        # Turn dec_init_states (a list of LSTMStateTuples) into a single
        # LSTMStateTuple for the batch

        feed = {
            self.enc_states: enc_states,
            self.enc_padding_mask: enc_padding_mask,
            self.dec_in_state: dec_init_states,
            self._dec_batch: latest_tokens,
        }

        to_return = {
          "ids": self._topk_ids,
          "probs": self._topk_log_probs,
          "states": self._dec_out_state,
          "attn_dists": self.attn_dists,
          "final_dists": self.final_dists,
        }

        if FLAGS.pointer_gen:
            feed[self.enc_batch_extend_vocab] = enc_batch_extend_vocab
            feed[self.max_art_oovs] = max_art_oovs
            to_return['p_gens'] = self.p_gens

        if self.hps.coverage:
            feed[self.prev_coverage] = prev_coverage
            to_return['coverage'] = self.coverage

        results = sess.run(to_return, feed_dict=feed)  # run the decoder step

        # Convert results['states'] (a single LSTMStateTuple) into a list of
        # LSTMStateTuple -- one for each hypothesis

        # Convert singleton list containing a tensor to a list of k arrays
        assert len(results['attn_dists']) == 1
        attn_dists = results['attn_dists'][0].tolist()

        if FLAGS.pointer_gen:
            # Convert singleton list containing a tensor to a list of k arrays
            assert len(results['p_gens']) == 1
            p_gens = results['p_gens'][0].tolist()
        else:
            p_gens = [None for _ in xrange(FLAGS.beam_size)]

        # Convert the coverage tensor to a list length k containing the coverage
        # vector for each hypothesis
        if FLAGS.coverage:
            new_coverage = results['coverage'].tolist()
            assert len(new_coverage) == FLAGS.beam_size
        else:
            new_coverage = [None for _ in xrange(FLAGS.beam_size)]

        return results['ids'], results['probs'], results['states'], attn_dists, p_gens, new_coverage

    def g_optimizer(self, *args, **kwargs):
        return tf.train.AdamOptimizer(*args, **kwargs)


def _mask_and_avg(values, padding_mask):
    """Applies mask to values then returns overall average (a scalar)

    Args:
      values: a list length max_dec_steps containing arrays shape (batch_size).
      padding_mask: tensor shape (batch_size, max_dec_steps) containing 1s and
      0s.

    Returns:
      a scalar
    """

    dec_lens = tf.reduce_sum(padding_mask, axis=1)  # shape batch_size. float32
    values_per_step = [v * padding_mask[:, dec_step] for dec_step, v in enumerate(values)]
    # shape (batch_size); normalized value for each batch member
    values_per_ex = sum(values_per_step)/dec_lens
    return tf.reduce_mean(values_per_ex)  # overall average


def _coverage_loss(attn_dists, padding_mask):
    """Calculates the coverage loss from the attention distributions.

    Args:
      attn_dists: The attention distributions for each decoder timestep. A list
      length max_dec_steps containing shape (batch_size, attn_length)
      padding_mask: shape (batch_size, max_dec_steps).

    Returns:
      coverage_loss: scalar
    """
    coverage = tf.zeros_like(
        attn_dists[0])
    # shape (batch_size, attn_length). Initial coverage is zero.
    # Coverage loss per decoder timestep. Will be list length max_dec_steps
    # containing shape (batch_size).
    covlosses = []
    for a in attn_dists:
        # calculate the coverage loss for this step
        covloss = tf.reduce_sum(tf.minimum(a, coverage), [1])
        covlosses.append(covloss)
        coverage += a  # update the coverage vector
    coverage_loss = _mask_and_avg(covlosses, padding_mask)
    return coverage_loss
