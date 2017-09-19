import tensorflow as tf
import numpy
import sys
from collections import OrderedDict
from cnn_discriminator import DisCNN
from nmt_generator import GenNmt
from share_function import prepare_data
from share_function import gen_force_train_iter
from share_function import FlushFile
from share_function import prepare_gan_dis_data
from share_function import prepare_sentence_to_maxlen
from share_function import deal_generated_y_sentence

from tensorflow.python.platform import tf_logging as logging

tf.app.flags.DEFINE_integer(
    'dim_word', 512, 'the dimension of the word embedding')
tf.app.flags.DEFINE_integer(
    'dim', 1024, 'the number of rnn units')
tf.app.flags.DEFINE_integer(
    'patience', 10, 'the patience for early stop')

tf.app.flags.DEFINE_integer(
    'max_epoches', 1, 'the max epoches for training')
tf.app.flags.DEFINE_integer('dis_epoches', 2, 'the max epoches for training')

tf.app.flags.DEFINE_integer(
    'gan_total_iter_num', 1, 'the max epoches for training')
tf.app.flags.DEFINE_integer(
    'gan_gen_iter_num', 1, 'the max epoches for training')
tf.app.flags.DEFINE_integer(
    'gan_dis_iter_num', 1, 'the max epoches for training')

tf.app.flags.DEFINE_integer(
    'dispFreq', 50, 'train for this many minibatches for displaying')
tf.app.flags.DEFINE_integer(
    'dis_dispFreq', 1,
    'train for this many minibatches for displaying discriminator')
tf.app.flags.DEFINE_integer(
    'gan_dispFreq', 50,
    'train for this many minibatches for displaying the gan gen training')
tf.app.flags.DEFINE_integer(
    'dis_saveFreq', 100,
    'train for this many minibatches for displaying discriminator')
tf.app.flags.DEFINE_integer(
    'gan_saveFreq', 100,
    'train for this many minibatches for displaying discriminator')
tf.app.flags.DEFINE_integer(
    'dis_devFreq', 100,
    'train for this many minibatches for displaying discriminator')
tf.app.flags.DEFINE_integer(
    'vocab_size', 80000, 'the size of the target vocabulary')

tf.app.flags.DEFINE_integer(
    'validFreq', 1000, 'train for this many minibatches for validation')
tf.app.flags.DEFINE_integer(
    'saveFreq', 2000, 'train for this many minibatches for saving model')
tf.app.flags.DEFINE_integer(
    'sampleFreq', 10000000, 'train for this many minibatches for sampling')

tf.app.flags.DEFINE_float('l2_r', 0.0001, 'L2 regularization penalty')
tf.app.flags.DEFINE_float('lr', 0.0001, 'learning rate')
tf.app.flags.DEFINE_float('alpha_c', 0.0, 'alignment regularization')
tf.app.flags.DEFINE_float('clip_c', 5, 'gradient clipping threshold')

tf.app.flags.DEFINE_integer(
    'max_len_s', 80, 'the max length of the training sentence')
tf.app.flags.DEFINE_integer(
    'max_leng', 15,
    'the max length of the training sentence for discriminator')

tf.app.flags.DEFINE_integer(
    'batch_size', 60, 'the size of the minibatch for training')
tf.app.flags.DEFINE_integer(
    'dis_batch_size', 10,
    'the size of the minibatch for training discriminator ')
tf.app.flags.DEFINE_integer(
    'gen_batch_size', 1, 'the size of the minibatch for training generator ')
tf.app.flags.DEFINE_integer(
    'gan_gen_batch_size', 2,
    'the size of the minibatch for training generator ')
tf.app.flags.DEFINE_integer(
    'gan_dis_batch_size', 1,
    'the size of the minibatch for training generator ')

tf.app.flags.DEFINE_integer(
    'valid_batch_size', 10, 'the size of the minibatch for validation')

tf.app.flags.DEFINE_string('optimizer', 'adadelta', 'the optimizing method')

tf.app.flags.DEFINE_string(
    'saveto', './gen_model/lcsts', 'the file name used to store the model')
tf.app.flags.DEFINE_string(
    'dis_saveto', './dis_model/lcsts',
    'the file name used to store the model of the discriminator')

tf.app.flags.DEFINE_string(
    'train_data_source', './data_1000w_golden/source_u8.txt.shuf',
    'the train data set of the soruce side')
tf.app.flags.DEFINE_string(
    'train_data_target', './data_1000w_golden/target_u8.txt.shuf',
    'the train data set of the target side')

tf.app.flags.DEFINE_string(
    'dis_positive_data', './data_test1000/positive.txt.shuf',
    'the positive train data set for the discriminator')
tf.app.flags.DEFINE_string(
    'dis_negative_data', './data_test1000/negative.txt.shuf',
    'the negative train data set for the discriminator')
tf.app.flags.DEFINE_string(
    'dis_source_data', './data_test1000/source.txt.shuf',
    'the negative train data set for the discriminator')

tf.app.flags.DEFINE_string(
    'dis_dev_positive_data', './data_gan_100w_fromZxw/dev_positive_u8.txt',
    'the positive train data set for the discriminator')
tf.app.flags.DEFINE_string(
    'dis_dev_negative_data', './data_gan_100w_fromZxw/dev_negative_u8.txt',
    'the negative train data set for the discriminator')
tf.app.flags.DEFINE_string(
    'dis_dev_source_data', './data_gan_100w_fromZxw/dev_source_u8.txt',
    'the negative train data set for the discriminator')

tf.app.flags.DEFINE_string(
    'gan_gen_source_data', './data_test1000/gan_gen_source_u8.txt',
    'the positive train data set for the discriminator')
tf.app.flags.DEFINE_string(
    'gan_dis_source_data', './data_gan_100w_fromZxw/gan_dis_source_u8.txt',
    'the positive train data set for the discriminator')
tf.app.flags.DEFINE_string(
    'gan_dis_positive_data', './data_gan_100w_fromZxw/gan_dis_positive_u8.txt',
    'the positive train data set for the discriminator')
tf.app.flags.DEFINE_string(
    'gan_dis_negative_data', './data_gan_100w_fromZxw/gan_dis_negative_u8.txt',
    'the negative train data set for the discriminator')

tf.app.flags.DEFINE_string(
    'valid_data_source', 'data/zhyang/dl4mt/source.txt',
    'the valid data set of the soruce size')
tf.app.flags.DEFINE_string(
    'valid_data_target', 'data/zhyang/dl4mt/target.txt',
    'the valid data set of the target side')

tf.app.flags.DEFINE_string(
    'dict_path', './vocab', "the vocabulary")

tf.app.flags.DEFINE_boolean(
    'use_dropout', False, 'whether to use dropout')
tf.app.flags.DEFINE_boolean(
    'gen_reload', False, 'whether to reload the generate model from model file')
tf.app.flags.DEFINE_boolean(
    'dis_reload', False,
    'whether to reload the discriminator model from model file')

tf.app.flags.DEFINE_boolean(
    'reshuffle', False, 'whether to reshuffle train data')
tf.app.flags.DEFINE_boolean(
    'dis_reshuffle', False,
    'whether to reshuffle train data of the discriminator')
tf.app.flags.DEFINE_boolean(
    'gen_reshuffle', False, 'whether to reshuffle train data of the generator')
tf.app.flags.DEFINE_boolean(
    'gan_gen_reshuffle', False,
    'whether to reshuffle train data of the generator')
tf.app.flags.DEFINE_boolean(
    'gan_dis_reshuffle', False,
    'whether to reshuffle train data of the generator')

tf.app.flags.DEFINE_boolean('DebugMode', False, 'whether to debug')

tf.app.flags.DEFINE_string(
    'gpu_device', 'gpu-0',
    'this many gpus used to train the model')
tf.app.flags.DEFINE_string(
    'dis_gpu_device', 'gpu-0',
    'this many gpus used to train the generator model')

tf.app.flags.DEFINE_string(
    'cpu_device', 'cpu-0', 'this cpu used to train the model')
tf.app.flags.DEFINE_string(
    'init_device', '/cpu:0', 'this cpu used to train the model')

tf.app.flags.DEFINE_string('precision', 'float32', 'precision on GPU')

tf.app.flags.DEFINE_integer('rollnum', 16, 'the rollnum for rollout')
tf.app.flags.DEFINE_integer('generate_num', 200000, 'the rollnum for rollout')
tf.app.flags.DEFINE_float('bias_num', 0.5, 'the bias_num  for rewards')

tf.app.flags.DEFINE_boolean(
    'teacher_forcing', False,
    'whether to do use teacher forcing for training the generator')
tf.app.flags.DEFINE_boolean(
    'is_gan_train', False, 'whether to do generative adversarial train')
tf.app.flags.DEFINE_boolean(
    'is_generator_train', True, 'whether to do generative adversarial train')
tf.app.flags.DEFINE_boolean(
    'is_discriminator_train', False,
    'whether to do generative adversarial train')
tf.app.flags.DEFINE_boolean(
    'is_decode', False, 'whether to decode')
tf.app.flags.DEFINE_boolean(
    'decode_is_print', False, 'whether to decode')
tf.app.flags.DEFINE_string(
    'decode_gpu', '/gpu:0', 'the device used to decode')

tf.app.flags.DEFINE_string(
    'decode_file', '/home/lerner/data/LCSTS/finished_files/test-art.txt',
    'the file to be decoded')
tf.app.flags.DEFINE_string(
    'decode_result_file', './data_test/negative.txt',
    'the file to save the decode results')

FLAGS = tf.app.flags.FLAGS

params = OrderedDict()

logging.set_verbosity(logging.INFO)


def main(argv):
    # -----------   create the session  -----------

    config = tf.ConfigProto()
    config.gpu_options.allow_growth = True
    config.gpu_options.per_process_gpu_memory_fraction = 0.9
    config.allow_soft_placement = True

    is_generator_train = FLAGS.is_generator_train
    is_decode = FLAGS.is_decode
    is_discriminator_train = FLAGS.is_discriminator_train
    is_gan_train = FLAGS.is_gan_train

    # -----------  pretraining  the generator -----------
    dict_path = FLAGS.dict_path
    batch_size = FLAGS.batch_size
    train_data_source = FLAGS.train_data_source
    train_data_target = FLAGS.train_data_target
    gpu_device = FLAGS.gpu_device
    dim_word = FLAGS.dim_word
    vocab_size = FLAGS.vocab_size
    dim = FLAGS.dim
    max_len_s = FLAGS.max_len_s
    max_leng = FLAGS.max_leng
    optimizer = FLAGS.optimizer
    precision = FLAGS.precision
    clip_c = FLAGS.clip_c
    max_epoches = FLAGS.max_epoches
    reshuffle = FLAGS.reshuffle
    saveto = FLAGS.saveto
    saveFreq = FLAGS.saveFreq
    dispFreq = FLAGS.dispFreq
    sampleFreq = FLAGS.sampleFreq
    gen_reload = FLAGS.gen_reload

    gan_gen_batch_size = FLAGS.gan_gen_batch_size

    sess = tf.Session(config=config)
    writer = tf.summary.FileWriter('./log', sess.graph)
    with tf.variable_scope('second_generate'):
        generator = GenNmt(
            sess=sess,
            batch_size=batch_size,
            dict_path=dict_path,
            train_data_source=train_data_source,
            train_data_target=train_data_target,
            vocab_size=vocab_size,
            gpu_device=gpu_device,
            dim_word=dim_word,
            dim=dim,
            max_len_s=max_len_s,
            max_leng=max_leng,
            clip_c=clip_c,
            max_epoches=max_epoches,
            reshuffle=reshuffle,
            saveto=saveto,
            saveFreq=saveFreq,
            dispFreq=dispFreq,
            sampleFreq=sampleFreq,
            optimizer=optimizer,
            precision=precision,
            gen_reload=gen_reload)

        if is_decode:
            decode_file = FLAGS.decode_file
            decode_result_file = FLAGS.decode_result_file
            decode_gpu = FLAGS.decode_gpu
            decode_is_print = FLAGS.decode_is_print
            print(
                'decoding the file %s on %s' % (
                    decode_file, decode_gpu))
            generator.gen_sample(
                decode_file, decode_result_file, 10,
                is_print=decode_is_print, gpu_device=decode_gpu)

            return 0

        elif is_generator_train:
            print('train the model and build the generate')
            generator.build_train_model()
            generator.gen_train()
            generator.build_generate(
                maxlen=max_leng, generate_batch=gan_gen_batch_size,
                optimizer='rmsprop')
            generator.rollout_generate(generate_batch=gan_gen_batch_size)
            print('done')

        else:
            print('build the generate without training')
            generator.build_train_model()
            generator.build_generate(
                maxlen=max_leng,
                generate_batch=gan_gen_batch_size,
                optimizer='rmsprop')
            generator.rollout_generate(generate_batch=gan_gen_batch_size)
            generator.init_and_reload()


# ----------- pretraining the discriminator -----------

    if is_discriminator_train or is_gan_train:

        dis_max_epoches = FLAGS.dis_epoches
        dis_dispFreq = FLAGS.dis_dispFreq
        dis_saveFreq = FLAGS.dis_saveFreq
        dis_devFreq = FLAGS.dis_devFreq
        dis_batch_size = FLAGS.dis_batch_size
        dis_saveto = FLAGS.dis_saveto
        dis_reshuffle = FLAGS.dis_reshuffle
        dis_gpu_device = FLAGS.dis_gpu_device
        max_leng = FLAGS.max_leng
        dis_positive_data = FLAGS.dis_positive_data
        dis_negative_data = FLAGS.dis_negative_data
        dis_source_data = FLAGS.dis_source_data
        dis_dev_positive_data = FLAGS.dis_dev_positive_data
        dis_dev_negative_data = FLAGS.dis_dev_negative_data
        dis_dev_source_data = FLAGS.dis_dev_source_data
        dis_reload = FLAGS.dis_reload

        filter_sizes_s = [i for i in range(1, max_len_s, 4)]
        num_filters_s = [(100 + i*10) for i in range(1, max_len_s, 4)]

        dis_filter_sizes = [i for i in range(1, max_leng, 4)]
        dis_num_filters = [(100 + i*10) for i in range(1, max_leng, 4)]

        discriminator = DisCNN(
            sess=sess,
            writer=writer,
            max_len_s=max_len_s,
            max_leng=max_leng,
            num_classes=2,
            dict_path=dict_path,
            vocab_size=vocab_size,
            batch_size=dis_batch_size,
            dim_word=dim_word,
            filter_sizes=dis_filter_sizes,
            num_filters=dis_num_filters,
            filter_sizes_s=filter_sizes_s,
            num_filters_s=num_filters_s,
            gpu_device=dis_gpu_device,
            positive_data=dis_positive_data,
            negative_data=dis_negative_data,
            source_data=dis_source_data,
            dev_positive_data=dis_dev_positive_data,
            dev_negative_data=dis_dev_negative_data,
            dev_source_data=dis_dev_source_data,
            max_epoches=dis_max_epoches,
            dispFreq=dis_dispFreq,
            saveFreq=dis_saveFreq,
            devFreq=dis_devFreq,
            saveto=dis_saveto,
            reload_mod=dis_reload,
            clip_c=clip_c,
            optimizer='rmsprop',
            reshuffle=dis_reshuffle,
            scope='disCNN')

        if is_discriminator_train:
            print('train the discriminator')
            discriminator.train()
            print('done')

        else:
            print('building the discriminator without training done')
            print('done')

    #   ----------- Start Reinforcement Training -----------
        if is_gan_train:

            gan_total_iter_num = FLAGS.gan_total_iter_num
            gan_gen_iter_num = FLAGS.gan_gen_iter_num
            gan_dis_iter_num = FLAGS.gan_dis_iter_num

            gan_gen_reshuffle = FLAGS.gan_gen_reshuffle

            gan_dis_source_data = FLAGS.gan_dis_source_data
            gan_dis_positive_data = FLAGS.gan_dis_positive_data
            gan_dis_negative_data = FLAGS.gan_dis_negative_data
            gan_dispFreq = FLAGS.gan_dispFreq
            gan_saveFreq = FLAGS.gan_saveFreq
            roll_num = FLAGS.rollnum
            generate_num = FLAGS.generate_num
            bias_num = FLAGS.bias_num
            teacher_forcing = FLAGS.teacher_forcing

            print('reinforcement training begin...')

            for gan_iter in range(gan_total_iter_num):

                print('reinforcement training for %d epoch' % gan_iter)
                gen_train_it = gen_force_train_iter(
                    gan_dis_source_data,
                    gan_dis_positive_data,
                    gan_gen_reshuffle,
                    generator.vocab,
                    vocab_size,
                    gan_gen_batch_size,
                    max_len_s,
                    max_leng
                )

                print('finetune the generator begin...')
                for gen_iter in range(gan_gen_iter_num):

                    x, y_ground, _ = next(gen_train_it)
                    # source, target
                    x_to_maxlen = prepare_sentence_to_maxlen(x, max_len_s)

                    x, x_mask, y_ground, y_ground_mask = prepare_data(
                        x, y_ground, max_len_s=max_len_s,
                        max_leng=max_leng, vocab_size=vocab_size
                    )
                    y_sample_out = generator.generate_step(x, x_mask)

                    y_input, y_input_mask = deal_generated_y_sentence(
                        y_sample_out, generator.vocab, precision=precision)
                    rewards = generator.get_reward(
                        x, x_mask, x_to_maxlen, y_input, y_input_mask,
                        roll_num, discriminator, bias_num=bias_num)
                    loss = generator.generate_step_and_update(
                        x, x_mask, y_input, rewards)
                    if gen_iter % gan_dispFreq == 0:
                        print(
                            'the %d iter, seen %d examples, loss is %f ' % (
                                gen_iter, (
                                    (gan_iter) * gan_gen_iter_num + gen_iter + 1
                                ), loss)
                        )
                    if gen_iter % gan_saveFreq == 0:
                        generator.saver.save(
                            generator.sess, generator.saveto)
                        print(
                            'save the parameters when seen %d examples ' % (
                                (gan_iter) * gan_gen_iter_num + gan_iter + 1
                            ))

                    # teacher force training
                    if teacher_forcing:
                        y_ground = prepare_sentence_to_maxlen(
                            numpy.transpose(y_ground), maxlen=max_leng,
                            precision=precision)
                        y_ground_mask = prepare_sentence_to_maxlen(
                            numpy.transpose(y_ground_mask), maxlen=max_leng,
                            precision=precision)
                        rewards_ground = numpy.ones_like(y_ground)
                        rewards_ground = rewards_ground * y_ground_mask
                        rewards_ground = numpy.transpose(rewards_ground)
                        print(
                            'the reward for ground in teacher forcing is ',
                            rewards_ground)
                        loss = generator.generate_step_and_update(
                            x, x_mask, y_ground, rewards_ground)
                        if gen_iter % gan_dispFreq == 0:
                            print(
                                'the %d iter, seen %d ground examples,\
                                loss is %f ' % (gen_iter, (
                                    (gan_iter) * gan_gen_iter_num +
                                    gen_iter + 1), loss))

                generator.saver.save(generator.sess, generator.saveto)
                print('finetune the generator done!')

                print('prepare the gan_dis_data begin ')
                data_num = prepare_gan_dis_data(
                    train_data_source, train_data_target,
                    gan_dis_source_data, gan_dis_positive_data,
                    num=generate_num, reshuf=True)
                print(
                    'prepare the gan_dis_data done, \
                    the num of the gan_dis_data is %d' % data_num)

                print(
                    'generator generate and save to %s'
                    % gan_dis_negative_data)
                generator.generate_and_save(
                    gan_dis_source_data, gan_dis_negative_data,
                    generate_batch=gan_gen_batch_size
                )
                print('done!')

                print('prepare the dis_dev sets')
                print('done!')

                print('finetune the discriminator begin...')
                discriminator.train(
                    max_epoch=gan_dis_iter_num,
                    positive_data=gan_dis_positive_data,
                    negative_data=gan_dis_negative_data,
                    source_data=gan_dis_source_data)
                discriminator.saver.save(
                    discriminator.sess, discriminator.saveto)
                print('finetune the discriminator done!')

            print('reinforcement training done')


if __name__ == '__main__':
    sys.stdout = FlushFile(sys.stdout)
    tf.app.run()
