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

import time
import numpy as np
import tensorflow as tf
from termcolor import colored
from attention_decoder import conv_attention_decoder
from utils import conv_encoder
from utils import my_gather
from utils import linear_mapping_weightnorm
from codecs import open
from data import PAD_TOKEN, UNKNOWN_TOKEN, START_DECODING, STOP_DECODING

FLAGS = tf.app.flags.FLAGS
# myclient = pymongo.MongoClient("mongodb://localhost:27017/")
# mydb = myclient["mydatabase"]
# mycol = mydb["bytecup2018"]


class PointerGenerator(object):
    """A class to represent a sequence-to-sequence model for text summarization.
    Supports both baseline mode, pointer-generator mode, and coverage"""

    def __init__(self, hps, enc_vocab, dec_vocab):
        self.hps = hps
        self._enc_vocab = enc_vocab
        self._dec_vocab = dec_vocab
        self._log_writer = open("./pg_log", "a", "utf-8")
        vocab_ = tf.convert_to_tensor(self._dec_vocab.id_keys)
        self._unk_mask = tf.where(
            tf.equal(vocab_, self._dec_vocab.word2id(UNKNOWN_TOKEN)),
            tf.zeros_like(vocab_, tf.float32), tf.ones_like(vocab_, tf.float32)
        )

    def _add_placeholders(self):
        """Add placeholders to the graph. These are entry points for any input
        data."""
        hps = self.hps
        batch_size = None
        if hps.mode in ["decode", "train_gan"]:
            max_dec_steps = None
        else:
            max_dec_steps = hps.max_dec_steps

        self.enc_batch = tf.placeholder(tf.int32, [batch_size, None], name='enc_batch')
        self.temp_batch = tf.placeholder(tf.int32, [batch_size, None], name='temp_batch_for_embedding')
        # self.enc_lens = tf.placeholder(tf.int32, [batch_size], name='enc_lens')
        self.enc_padding_mask = tf.placeholder(tf.float32, [batch_size, None], name='enc_padding_mask')

        self._dec_batch = tf.placeholder(tf.int32, [batch_size, max_dec_steps], name='dec_batch')
        self.target_batch = tf.placeholder(tf.int32, [batch_size, hps.max_dec_steps], name='target_batch')
        self.dec_padding_mask = tf.placeholder(tf.float32, [batch_size, hps.max_dec_steps], name='decoder_padding_mask')
        if FLAGS.pointer_gen:
            self._enc_batch_extend_vocab = tf.placeholder(tf.int32, [hps.batch_size, None], name='enc_batch_extend_vocab')
            self._max_art_oovs = tf.placeholder(tf.int32, [], name='max_art_oovs')

        self.cell_c = tf.placeholder(
            tf.float32, shape=[batch_size, self.hps.hidden_dim])
        self.cell_h = tf.placeholder(
            tf.float32, shape=[batch_size, self.hps.hidden_dim])

        self.k_sample_targets = tf.placeholder(tf.int32, [FLAGS.sample_num, batch_size, hps.max_dec_steps], name='k_sample_targets')
        self.k_sample_targets_mask = tf.placeholder(tf.float32, [FLAGS.sample_num, batch_size, hps.max_dec_steps], name='k_padding_mask_of_the_sample_targets')
        self.k_samples = tf.placeholder(tf.int32, [FLAGS.sample_num, batch_size, hps.max_dec_steps], name='k_samples')
        self.k_rewards = tf.placeholder(tf.float32, shape=[FLAGS.sample_num, batch_size, hps.max_dec_steps], name="k_rewards")

        self._eval_dec_batch = tf.placeholder(tf.int32, [batch_size, hps.max_dec_steps], name='eval_dec_batch')

        if hps.mode in ["decode", 'train_gan'] and hps.coverage:
            self.prev_coverage = tf.placeholder(tf.float32, [None, None], name='prev_coverage')

    def _make_feed_dict(self, batch, just_enc=False, gan_eval=False, gan=False):
        if gan_eval:
            gan = True
        feed_dict = {}
        feed_dict[self.enc_batch] = batch.enc_batch
        feed_dict[self.enc_stem] = batch.stem_batch
        feed_dict[self.enc_pos] = batch.pos_batch
        feed_dict[self.enc_ner] = batch.ner_batch
        feed_dict[self.enc_tfidf] = batch.tfidf_batch
        if FLAGS.hierarchical_enc:
            feed_dict[self.enc_phrase_idx] = batch.phrase_label_batch
            feed_dict[self.enc_sent_idx] = batch.sent_label_batch
        # feed_dict[self.enc_lens] = batch.lens_batch
        feed_dict[self.enc_padding_mask] = batch.padding_mask_batch
        if FLAGS.pointer_gen:
            feed_dict[self._enc_batch_extend_vocab] = batch.enc_batch_extend_vocab
            feed_dict[self._max_art_oovs] = batch.max_art_oovs

        if not just_enc:
            feed_dict[self.target_batch] = batch.target_batch
            feed_dict[self.dec_padding_mask] = batch.dec_padding_mask
            if gan_eval:
                feed_dict[self._eval_dec_batch] = batch.dec_batch
            elif not gan:
                feed_dict[self._dec_batch] = batch.dec_batch
        return feed_dict

    def _add_seq2seq(self):
        """Add the whole sequence-to-sequence model to the graph."""
        hps = self.hps

        with tf.name_scope('seq2seq'):
            self.rand_unif_init = tf.random_uniform_initializer(
                -hps.rand_unif_init_mag, hps.rand_unif_init_mag, seed=123)
            self.trunc_norm_init = tf.truncated_normal_initializer(stddev=hps.trunc_norm_init_std)

            k_samples_ls = tf.unstack(self.k_samples, axis=0)
            k_sample_targets_ls = tf.unstack(self.k_sample_targets, axis=0)
            k_sample_targets_mask_ls = tf.unstack(self.k_sample_targets_mask, axis=0)
            k_rewards_ls = tf.unstack(self.k_rewards, axis=0)

            with tf.variable_scope('embeddings'):

                self.shared_embeddings = tf.get_variable(
                    'shared_part_emb', dtype=tf.float32, initializer=np.load('./data/shared_part_embeddings.npy'))
                self.enc_part_embeddings = tf.get_variable(
                    'enc_part_emb', dtype=tf.float32, initializer=np.load('./data/enc_part_embeddings.npy'))
                self.dec_part_embeddings = tf.get_variable(
                    'dec_part_emb', dtype=tf.float32, initializer=np.load('./data/dec_part_embeddings.npy'))

                self.enc_embeddings = tf.concatenate([self.shared_embeddings, self.enc_part_embeddings], 0)
                self.dec_embeddings = tf.concatenate([self.shared_embeddings, self.dec_part_embeddings], 0)

                self.pos_embeddings = tf.get_variable(
                    'pos_embeddings', [46, 10], dtype=tf.float32, initializer=self.trunc_norm_init)
                self.ner_embeddings = tf.get_variable(
                    'ner_embeddings', [25, 10], dtype=tf.float32, initializer=self.trunc_norm_init)

                emb_enc_inputs = tf.nn.embedding_lookup(self.enc_embeddings, self.enc_batch) + \
                    tf.nn.embedding_lookup(self.enc_embeddings, self.enc_stem)
                emb_enc_pos = tf.nn.embedding_lookup(self.pos_embeddings, self.enc_pos)
                emb_enc_ner = tf.nn.embedding_lookup(self.ner_embeddings, self.enc_ner)
                self.emb_enc_inputs = tf.concat([emb_enc_inputs, emb_enc_pos, emb_enc_ner, tf.expand_dim(self.enc_tfidf, axis=-1)], axis=-1)

                self.temp_embedded_seq = tf.nn.embedding_lookup(self.enc_embeddings, self.temp_batch)
                emb_dec_inputs = tf.nn.embedding_lookup(self.dec_embeddings, self._dec_batch)
                emb_eval_dec_inputs = tf.nn.embedding_lookup(self.dec_embeddings, self._eval_dec_batch)

                k_emb_samples_ls = [
                    tf.nn.embedding_lookup(self.dec_embeddings, samples)
                    for samples in k_samples_ls
                ]

            attention_keys, dec_in_state, phrase_keys, sent_keys = conv_encoder(
                self.emb_enc_inputs,
                hps.mode in ["pretrain_gen", "train_gan"],
                indices={"phrase": self.enc_phrase_idx, "sent": self.enc_sent_idx} if self.hierarchical_enc else None)

            self.attention_keys = attention_keys
            self.attention_values = (
                linear_mapping_weightnorm(
                    self.attention_keys, self.emb_enc_inputs.get_shape()[-1].value, var_scope_name="attention_key2value"
                ) + self.emb_enc_inputs) * tf.sqrt(0.5)

            phrase_emb = my_gather(self.emb_enc_inputs, self.enc_phrase_idx)
            self.phrase_values = (
                linear_mapping_weightnorm(
                    phrase_keys, phrase_emb.get_shape()[-1].value, var_scope_name="phrase_attention_key2value"
                ) + phrase_emb) * tf.sqrt(0.5)

            sent_emb = my_gather(self.emb_enc_inputs, self.enc_sent_idx)
            self.phrase_values = (
                linear_mapping_weightnorm(
                    sent_keys, sent_emb.get_shape()[-1].value, var_scope_name="sent_attention_key2value"
                ) + sent_emb) * tf.sqrt(0.5)

            with tf.variable_scope('decoder') as decoder_scope:
                is_training = False if self.hps.mode in ["train_gan", 'decode'] else True
                final_dists = self._conv_decoder(emb_dec_inputs, is_training=is_training)
                if FLAGS.pointer_gen:
                    final_dists = self._calc_final_dist(final_dists, self.attn_dists)
                decoder_scope.reuse_variables()
                self.final_dists = final_dists
                self.topk_log_probs, self.indices = tf.nn.top_k(tf.log(self.final_dists[0]), self.hps.beam_size * 2)
                self._ran_id = tf.multinomial(tf.log(self.final_dists[0]), 1)

                eval_final_dists = self._conv_decoder(emb_eval_dec_inputs, is_training=True)

                k_sample_final_dists_ls = []
                for emb_samples in k_emb_samples_ls:
                    sample_final_dists = self._conv_decoder(emb_samples, is_training=True)
                    k_sample_final_dists_ls.append(sample_final_dists)

            def get_loss(final_dists, target_batch, padding_mask, rewards=None):
                batch_nums = tf.range(0, limit=tf.shape(target_batch)[0])

                loss_per_step = []
                for dec_step, dist in enumerate(final_dists):
                    targets = target_batch[:, dec_step]
                    indices = tf.stack((batch_nums, targets), axis=1)
                    gold_probs = tf.gather_nd(dist, indices)
                    losses = -tf.log(gold_probs) * padding_mask[:, dec_step]
                    loss_per_step.append(losses * rewards[:, dec_step] if rewards is not None else losses)
                return loss_per_step

            with tf.variable_scope('generator_loss'):

                tf.Print(self.final_dists, self.final_dists, "final list")
                loss_per_step = get_loss(self.final_dists, self.target_batch, self.dec_padding_mask)
                eval_loss_per_step = get_loss(eval_final_dists, self.target_batch, self.dec_padding_mask)
                self._loss = _avg(loss_per_step, self.dec_padding_mask)
                self._eval_loss = _avg(eval_loss_per_step, self.dec_padding_mask)

                if hps.coverage:
                    with tf.variable_scope('coverage_loss'):
                        self._coverage_loss = _coverage_loss(
                            self.attn_dists, self.dec_padding_mask)
                    self._total_loss = \
                        self._loss + hps.cov_loss_wt * self._coverage_loss

            with tf.variable_scope('gan_loss'):
                if hps.mode == "train_gan":
                    k_gan_losses = []
                    for k in range(len(k_sample_targets_ls)):
                        gan_loss_per_step = get_loss(
                            k_sample_final_dists_ls[k], k_sample_targets_ls[k],
                            k_sample_targets_mask_ls[k], k_rewards_ls[k])
                        masked_average = _avg(gan_loss_per_step, k_sample_targets_mask_ls[k])
                        k_gan_losses.append(masked_average)

                    self.gan_loss = tf.reduce_mean(tf.stack(k_gan_losses))

        loss_to_minimize = self._total_loss if self.hps.coverage else self._loss
        trainable_variables = tf.trainable_variables()
        gradients = tf.gradients(
            loss_to_minimize, trainable_variables,
            aggregation_method=tf.AggregationMethod.EXPERIMENTAL_TREE)

        with tf.device("/gpu:0"):
            grads, global_norm = tf.clip_by_global_norm(
                gradients, self.hps.gen_max_gradient)

        self.learning_rate = tf.train.exponential_decay(
            self.hps.gen_lr,               # Base learning rate.
            self.global_step * self.hps.batch_size,  # Current index into the dataset.
            1000000,             # Decay step.
            0.95,                # Decay rate.
            staircase=True)
        optimizer = tf.train.AdamOptimizer(self.learning_rate)
        with tf.device("/gpu:0"):
            self._train_op = optimizer.apply_gradients(
                zip(grads, trainable_variables),
                global_step=self.global_step)

        if hps.mode == "train_gan":
            g_opt = self.g_optimizer(FLAGS.gan_lr)
            trainable_variables = tf.trainable_variables()
            gradients = tf.gradients(self.gan_loss, trainable_variables,
                                     aggregation_method=tf.AggregationMethod.EXPERIMENTAL_TREE)
            self.g_grad, _ = tf.clip_by_global_norm(gradients, self.hps.gen_max_gradient)
            with tf.device("/gpu:0"):
                self.g_updates = g_opt.apply_gradients(zip(self.g_grad, trainable_variables), global_step=self.global_step)

        return decoder_scope

    def get_cur_lr(self, sess):
        return sess.run(self.learning_rate)

    def _conv_decoder(self, emb_dec_inputs,
                      attention_keys=None, attention_values=None, enc_padding_mask=None, is_training=True, mask=True):

        vsize = self.hps.dec_vocab_size
        logits = conv_attention_decoder(
            emb_dec_inputs, enc_padding_mask, attention_keys, attention_values, vsize, is_training)

        if is_training:
            vocab_dists = tf.unstack(tf.nn.softmax(logits), axis=1)
        elif mask is True:
            vocab_dists = [tf.nn.softmax(logits) * self._unk_mask]
        else:
            vocab_dists = [tf.nn.softmax(logits)]

        return vocab_dists

    def build_graph(self):
        """Add the placeholders, model, global step, train_op and summaries to
        the graph"""
        t0 = time.time()
        self._add_placeholders()
        self.global_step = tf.Variable(0, name='global_step', trainable=False)
        with tf.device("/gpu:0"):
            decoder_scope = self._add_seq2seq()
        self.least_val_loss = tf.Variable(1000.0, name='least_val_loss', trainable=False)
        t1 = time.time()
        print(colored('Time to build graph: %s seconds' % (t1 - t0), "yellow"))
        return decoder_scope

    def run_one_batch(self, sess, batch, update=True, gan_eval=False):
        """Runs one training iteration. Returns a dictionary containing train
        op, summaries, loss, global_step and (optionally) coverage loss."""
        if gan_eval:
            update = False

        feed_dict = self._make_feed_dict(batch, gan_eval=gan_eval)

        to_return = {
            'global_step': self.global_step,
        }

        if gan_eval:
            to_return['loss'] = self._eval_loss
        else:
            to_return['loss'] = self._loss
        if update:
            to_return['train_op'] = self._train_op
        if self.hps.coverage:
            to_return['coverage_loss'] = self._coverage_loss
        rsts = sess.run(to_return, feed_dict)

        return rsts

    def run_gan_batch(self, sess, batch, samples, sample_targets,
                      sample_padding_mask, rewards, update=True, gan_eval=False
                      ):
        feed_dict = self._make_feed_dict(batch, gan_eval=gan_eval, gan=True)

        feed_dict.update({
            self.k_samples: samples,
            self.k_sample_targets: sample_targets,
            self.k_sample_targets_mask: sample_padding_mask,
            self.k_rewards: rewards,
        })

        to_return = {
            'global_step': self.global_step,
            'loss': self.gan_loss,
        }
        if update:
            to_return['updates'] = self.g_updates
        results = sess.run(to_return, feed_dict)
        return results

    def run_encoder(self, sess, batch):
        feed_dict = self._make_feed_dict(batch, just_enc=True)
        to_return = {
            "attention_values": self.attention_values,
            "attention_keys": self.attention_keys,
        }
        results = sess.run(to_return, feed_dict)

        return results['attention_keys'], results['attention_values']

    def decode_onestep(self, emb_dec_inputs):
        """
        function: decode onestep for rollout
        inputs:
            the embedded input
        """
        final_dists = self._conv_decoder(emb_dec_inputs, is_training=False, mask=False)
        final_dists = final_dists[0]
        output_id = tf.squeeze(tf.cast(tf.reshape(tf.multinomial(tf.log(final_dists), 1), [self.hps.batch_size]), tf.int32))
        return output_id

    def run_decode_onestep(self, sess, dec_inputs, attention_keys, attention_values, enc_padding_mask):

        feed = {
            self._dec_batch: dec_inputs,
            self.attention_keys: attention_keys,
            self.attention_values: attention_values,
            self.enc_padding_mask: enc_padding_mask,
        }

        to_return = {
          "topk_log_probs": self.topk_log_probs,
          "indices": self.indices,
          "ran_id": self._ran_id,
        }

        results = sess.run(to_return, feed_dict=feed)

        return results['topk_log_probs'], results['indices'], results['ran_id']

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
    values_per_ex = tf.reduce_sum(tf.stack(values_per_step, 1), 1)/dec_lens
    return tf.reduce_mean(values_per_ex)  # overall average


def _avg(values, padding_mask):
    """Applies mask to values then returns overall average (a scalar)

    Args:
      values: a list length max_dec_steps containing arrays shape (batch_size).
      padding_mask: tensor shape (batch_size, max_dec_steps) containing 1s and
      0s.

    Returns:
      a scalar
    """

    dec_lens = tf.reduce_sum(padding_mask, axis=1)  # shape batch_size. float32
    values_per_ex = tf.reduce_sum(tf.stack(values, 1), 1)/dec_lens
    return tf.reduce_mean(values_per_ex)  # overall average


def _mask(values, padding_mask):
    """Applies mask to values then returns overall average (a scalar)

    Args:
      values: a list length max_dec_steps containing arrays shape (batch_size).
      padding_mask: tensor shape (batch_size, max_dec_steps) containing 1s and
      0s.

    Returns:
      a scalar
    """

    values_per_step = [v * padding_mask[:, dec_step] for dec_step, v in enumerate(values)]
    values_per_ex = sum(values_per_step)
    return tf.reduce_sum(values_per_ex)  # overall loss


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
    covlosses = []
    for a in attn_dists:
        covloss = tf.reduce_sum(tf.minimum(a, coverage), [1])
        covlosses.append(covloss)
        coverage += a  # update the coverage vector
    coverage_loss = _mask_and_avg(covlosses, padding_mask)
    return coverage_loss
