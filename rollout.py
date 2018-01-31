from __future__ import unicode_literals, print_function
from __future__ import absolute_import
from __future__ import division
import tensorflow as tf
from tensorflow.python.ops import tensor_array_ops, control_flow_ops
# from tensorflow.python.ops import variable_scope
import numpy as np
from data import gen_vocab2dis_vocab
from data import strip_pads
import data
from utils import rouge_l
PAD_TOKEN = "[PAD]"
START_DECODING = '[START]'
STOP_DECODING = '[STOP]'
FLAGS = tf.app.flags.FLAGS


class Rollout(object):
    def __init__(self, generator, update_rate, decoder_scope):
        self.generator = generator
        self.update_rate = update_rate
        # TODO: for the variables update
        self._gen_hps = self.generator.hps
        self.g_embeddings = tf.identity(self.generator.embeddings)
        start_tokens = np.array([self.generator._vocab.word2id(data.START_DECODING)] * self._gen_hps.batch_size)
        emb_start_token = tf.nn.embedding_lookup(self.g_embeddings, start_tokens)
        next_input = emb_start_token
        #######################################################################

        # placeholder definition
        self.sample = tf.placeholder(
            tf.int32, shape=[self._gen_hps.batch_size, self._gen_hps.max_dec_steps], name="sample")
        self.cell_c = tf.placeholder(
            tf.float32, shape=[self._gen_hps.batch_size, self._gen_hps.hidden_dim], name="cell_c")
        self.cell_h = tf.placeholder(
            tf.float32, shape=[self._gen_hps.batch_size, self._gen_hps.hidden_dim], name="cell_h")
        self.given_num = tf.placeholder(tf.int32, name="given_num")
        # self.enc_states = tf.placeholder(
        #     tf.float32, shape=[self._gen_hps.batch_size, self._gen_hps.max_enc_steps,
        #                        self._gen_hps.hidden_dim])
        # this should be changed
        init_dec_in_state = tf.contrib.rnn.LSTMStateTuple(self.cell_c, self.cell_h)
        new_state = init_dec_in_state
        # sequence of tokens generated by generator

        # processed for batch
        self.emb_sample = tf.transpose(
            tf.nn.embedding_lookup(self.g_embeddings, self.sample), perm=[1, 0, 2])
        # seq_length x batch_size x emb_dim

        emb_sample_ar = tensor_array_ops.TensorArray(
            dtype=tf.float32, size=self._gen_hps.max_dec_steps)
        emb_sample_ar = emb_sample_ar.unstack(self.emb_sample)

        sample_ar = tensor_array_ops.TensorArray(dtype=tf.int32, size=self._gen_hps.max_dec_steps)
        # _sample = tf.slice(, [1, 0], [-1, -1])
        sample_ar = sample_ar.unstack(tf.transpose(self.sample, perm=[1, 0]))

        ######################################################################

        self.rollout_sample_ar = tensor_array_ops.TensorArray(
            dtype=tf.int32, size=self._gen_hps.max_dec_steps, dynamic_size=False, infer_shape=True)

        with tf.variable_scope(decoder_scope, reuse=True):
            def recurrence_given(i, dec_input, dec_in_state, given_num, rollout_sample_ar):
                next_input_id, new_state = self.generator.decode_onestep([dec_input], dec_in_state)
                emb_next_input = emb_sample_ar.read(i)
                rollout_sample_ar = rollout_sample_ar.write(i, sample_ar.read(i))
                return i+1, emb_next_input, new_state, given_num, rollout_sample_ar

            i, next_input, new_state, given_num, self.rollout_sample_ar = control_flow_ops.while_loop(
                cond=lambda i, _1, _2, given_num, _4: i < given_num,
                body=recurrence_given,
                loop_vars=(tf.constant(0, dtype=tf.int32), next_input,
                           new_state, self.given_num, self.rollout_sample_ar))

            def recurrence_rollout(i, dec_input, dec_in_state, rollout_sample_ar):
                output_id, new_state = self.generator.decode_onestep([dec_input], dec_in_state)
                rollout_sample_ar = rollout_sample_ar.write(i, output_id)
                next_input_id_without_oovs = tf.where(
                    tf.less(output_id, self._gen_hps.gen_vocab_size),
                    output_id, tf.constant(
                        [self.generator._vocab.word2id(data.UNKNOWN_TOKEN)] * self._gen_hps.batch_size))
                next_input_emb = tf.nn.embedding_lookup(self.g_embeddings, next_input_id_without_oovs)
                return i+1, next_input_emb, new_state, rollout_sample_ar

            _, _, _, self.rollout_sample_ar = control_flow_ops.while_loop(
                cond=lambda i, _1, _2, _3: i < self._gen_hps.max_dec_steps,
                body=recurrence_rollout,
                loop_vars=(i, next_input, new_state, self.rollout_sample_ar))

        self.rollout_sample_ar = self.rollout_sample_ar.stack()  # seq_length x batch_size
        self.rollout_sample_ar = tf.transpose(self.rollout_sample_ar, perm=[1, 0])
        # self.rollout_sample_ar = tf.stop_gradient(self.rollout_sample_ar)
        # batch_size x seq_length

    def get_reward(self, hps_gan, sess, gen_vocab, dis_vocab, source_batch,
                   enc_states, dec_in_state, k_samples, discriminator):
        # dec_in_state is [batch_size, hidden_dim * 2] and that should be
        # changed to [batch_size, hidden_dim] for the attention_decoder
        rollout_num = hps_gan.rollout_num
        rouge_ratio = hps_gan.rouge_reward_ratio

        article_oovs = source_batch.art_oovs
        articles = source_batch.enc_batch_extend_vocab
        batch_size = articles.shape[0]

        if self.generator.hps.vocab_type == "word" and discriminator.hps.vocab_type == "char":
            articles = gen_vocab2dis_vocab(
                articles, gen_vocab, article_oovs,
                dis_vocab, discriminator.hps.max_enc_steps, PAD_TOKEN)
        else:
            conditions = articles
            zeros = np.zeros((batch_size, discriminator.hps.max_enc_steps))
            zeros[:, :conditions.shape[1]] = conditions
            articles = zeros
        # abs_chars = np.array(gen_vocab2dis_vocab(
        #     source_batch.target_batch, gen_vocab, article_oovs,
        #     dis_vocab, self._gen_hps.max_dec_steps, STOP_DECODING))
        k_rewards = []

        for k, samples in enumerate(k_samples):
            dis_rewards = []
            rouge_rewards = np.zeros(self._gen_hps.max_dec_steps+1, batch_size)
            for ir in range(rollout_num):
                for given_num in range(hps_gan.rollout_start, self._gen_hps.max_dec_steps):

                    feed_dict = {}
                    feed_dict[self.sample] = samples
                    # this is the source
                    # feed_dict[self.generator.enc_lens] = source_batch.enc_lens
                    feed_dict[self.given_num] = given_num
                    feed_dict[self.generator.enc_states] = enc_states
                    feed_dict[self.generator.enc_padding_mask] = source_batch.enc_padding_mask
                    feed_dict[self.cell_c] = dec_in_state.c
                    feed_dict[self.cell_h] = dec_in_state.h
                    feed_dict[self.generator.enc_batch_extend_vocab] = articles
                    feed_dict[self.generator.max_art_oovs] = source_batch.max_art_oovs
                    # how to deal with the coverage?

                    # the unique feature for the pointer gen is the
                    # enc_batch_extend_vocab and the max_art_oovs
                    rollout_samples = sess.run(self.rollout_sample_ar, feed_dict)
                    # how about multiple generators for one discriminator?
                    if self.generator.hps.vocab_type == "word" and discriminator.hps.vocab_type == "char":
                        rollout_samples = gen_vocab2dis_vocab(
                            rollout_samples, gen_vocab, article_oovs,
                            dis_vocab, discriminator.hps.max_dec_steps, STOP_DECODING, articles, print_sample=False)

                    if rouge_ratio != 1:
                        if given_num != 0:
                            feed = {
                                discriminator.inputs: rollout_samples,
                                discriminator.conditions: articles}
                            ypred_for_auc = sess.run(discriminator.dis_ypred_for_auc, feed)
                            ypred = np.array([item[1] for item in ypred_for_auc])
                            if ir == 0:
                                dis_rewards.append(ypred)
                            else:
                                dis_rewards[given_num-1] = ypred

                    if rouge_ratio != 0:
                        rpred = rouge_l(strip_pads(rollout_samples.tolist(), gen_vocab.word2id(STOP_DECODING)),
                                        source_batch.dec_batch.tolist(), rs=rollout_samples)
                        dis_rewards[given_num] += np.array(rpred)

                if rouge_ratio != 1:
                    # the last token reward
                    if ir == 0 and k == 0:
                        ps = "multinomial in rollout"
                    else:
                        ps = False
                    if self.generator.hps.vocab_type == "word" and discriminator.hps.vocab_type == "char":
                        samples = gen_vocab2dis_vocab(
                            samples, gen_vocab, article_oovs,
                            dis_vocab, discriminator.hps.max_dec_steps, STOP_DECODING, print_sample=ps)

                    feed = {
                        discriminator.inputs: samples,
                        discriminator.conditions: articles}
                    ypred_for_auc = sess.run(discriminator.dis_ypred_for_auc, feed)
                    ypred = np.array([item[1] for item in ypred_for_auc])
                    if ir == 0:
                        dis_rewards.append(ypred)
                    else:
                        dis_rewards[self._gen_hps.max_dec_steps-1] += ypred
                if rouge_ratio:
                    rpred = rouge_l(strip_pads(samples, gen_vocab.word2id(STOP_DECODING)),
                                    source_batch.dec_batch.tolist(), rs=rollout_samples)
                    rouge_rewards[self._gen_hps.max_dec_steps] += np.array(rpred)

            rouge_rewards = np.transpose(rouge_rewards)
            dis_rewards = np.transpose(np.array(dis_rewards))

            if rouge_ratio != 0:
                rouge_rewards = rouge_rewards[:, 1:] - rouge_rewards[:, :-1]

            if rouge_ratio == 1:
                rewards = rouge_rewards
            elif rouge_ratio == 0:
                rewards = dis_rewards
            else:
                rewards = (1 - rouge_ratio)*dis_rewards + rouge_ratio*rouge_rewards

            k_rewards.append(rewards / (1.0 * rollout_num))
            # batch_size x seq_length

        return k_rewards
