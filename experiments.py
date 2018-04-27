from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import matplotlib  # todo
matplotlib.use('Agg')
import numpy as np
import os
import tensorflow as tf
import time

from data import make_minibatch
from distributions import dirichlet, gaussian, niw
from helpers.logging_utils import generate_log_id
from losses import weighted_mse, diagonal_gaussian_logprob, bernoulli_logprob, imputation_losses, \
    generate_missing_data_mask, purity
from models import svae
from helpers.tf_utils import average_gradients
from visualisation.visualise_svae import svae_dashboard
from helpers.scheduling import create_schedule
from helpers.tf_utils import variable_on_device


# global settings
path_dataset = 'datasets'
ratio_tr = 0.7
ratio_val = None
size_minibatch = 64
size_testbatch = -100  # if negative: full testset

nb_samples = 10  # nb samples for gradient computation
nb_samples_te = 100  # 100 for small datasets; 20 for mnist on laptop (OOM otherwise)
nb_samples_pert = 20  # how many perturbed samples to be generated per test data point (for missing data imputation)
ratio_missing_data = 0.1

nb_iters = 20000
measurement_freq = 500
plot_freq = 2500
imputation_freq = 25000
checkpoint_freq = 25000

nb_gpu = 1  # number of GPUs to be used
param_device = '/gpu:0'  # where parameters are stored
meas_device = '/gpu:0'   # where performance is evaluated

nb_threads = 2  # for input queue

stddev_init_nn = 0.01  # neural net initialization

log_dir = 'logs_svae'

verbose = False  # log device placement

# set size_minibatch=64
schedule = create_schedule({
    'dataset': 'gtex',
    'method': 'svae',
    'lr': [0.0003],    # adam stepsize
    'lrcvi': [0.2],    # cvi stepsize (convex combination)
    'decay_rate': [0.95],  # decreasing cvi stepsize
    'K': 10,           # nb components
    'L': [6],          # latent dimensionality
    'U': 50,           # hidden units
    'seed': 0
})

# set size_minibatch=100 (as in Johnson+, 2016); initialize phi_gmm with gmm_prior (line 181)
# schedule = create_schedule({
#     'dataset': 'pinwheel',
#     'method': 'svae-cvi',
#     'lr': [0.01],     # adam stepsize
#     'lrcvi': [0.1],   # cvi stepsize (convex combination)
#     'K': 10,           # nb components
#     'L': [2],          # latent dimensionality
#     'U': 50,           # hidden units
#     'seed': 0
# })

# init phi_PGM with gmm_prior; set m_scale=5.; don't standardize data; minibatch=100;
# schedule = create_schedule({
#     'dataset': 'pinwheel',
#     'method': ['svae-cvi-smm'],
#     'lr': [0.01],    # adam stepsize
#     'lrcvi': 0.1,    # cvi stepsize (convex combination)
#     'decay_rate': 1,
#     'delay': 0,
#     'K': 10,           # nb components
#     'L': 2,          # latent dimensionality
#     'U': 40,           # hidden units
#     'DoF': 5,
#     'seed': range(10)
# })


# for train/test split
seed_data = 0

###################################################################################################################

# iterate through scheduled experiments
for config_id, config in enumerate(schedule):

    print("Experiment %d with config\n%s\n" % (config_id, str(config)))

    # reset Tensorflow graph
    with tf.Graph().as_default(), tf.device(param_device):

        # set graph-level seed
        tf.set_random_seed(config['seed'])

        # binarise data to {-1, 1} for image datasets
        binarise_data = config['dataset'] in ['mnist', 'mnist-small']

        noise_level = config.get('noise_level', 0)

        y_tr, lbl_tr, y_te, lbl_te = make_minibatch(config['dataset'], ratio_tr=ratio_tr, ratio_val=ratio_val,
                                                    path_datadir=path_dataset, size_minibatch=size_minibatch,
                                                    size_testbatch=size_testbatch, nb_towers=nb_gpu,
                                                    nb_threads=nb_threads, seed_split=seed_data,
                                                    binarise=binarise_data, seed_minibatch=config['seed'],
                                                    dtype=tf.float32, noise_level=noise_level)


        # for error computation we need images in [0, 1] instead of {-1, 1}
        y_tr_01 = tf.concat(y_tr, axis=0)
        y_te_01 = y_te

        if binarise_data:
            y_tr_01 = tf.where(tf.equal(y_tr_01, -1),
                               tf.zeros_like(y_tr_01, dtype=tf.float32),
                               tf.ones_like(y_tr_01, dtype=tf.float32))
            y_te_01 = tf.where(tf.equal(y_te, -1),
                               tf.zeros_like(y_te, dtype=tf.float32),
                               tf.ones_like(y_te, dtype=tf.float32))

        # define nn-architecture
        decoder_type = 'bernoulli' if config['dataset'] in ['mnist', 'mnist-small'] else 'standard'
        encoder_layers = [(config['U'], tf.tanh), (config['U'], tf.tanh), (config['L'], 'natparam')]
        decoder_layers = [(config['U'], tf.tanh), (config['U'], tf.tanh), (int(y_te.get_shape()[1]), decoder_type)]

        # define step size (CVI step-size can be decreasing)
        with tf.name_scope('learning_rate'):
            global_step = tf.get_variable('global_step', [], initializer=tf.constant_initializer(0), trainable=False)
            decay_rate = config.get('decay_rate', 1)  # no decay by default
            lrcvi = tf.train.exponential_decay(config['lrcvi'], global_step, 1000, decay_rate, staircase=False)
            tf.summary.scalar('learning_rate_cvi', lrcvi)

        # optimiser
        opt = tf.train.AdamOptimizer(learning_rate=config['lr'], use_locking=True)
        # opt = tf.train.AdagradOptimizer(learning_rate=config['lr'], use_locking=True)

        # mu_theta and sigma_theta are point estimates in the SMM case.
        if 'smm' in config['method']:
            # init helper values for SMM (theta is a constant, we just need it to init the rec GMM below)
            gmm_prior, theta = svae.init_mm(config['K'], config['L'], seed=config['seed'], param_device=param_device,
                                            theta_as_variable=False)

            # create tensor for Student-t parameters
            with tf.variable_scope('theta'):
                mu_k, L_k = svae.make_loc_scale_variables(gmm_prior, param_device=param_device)
                DoF = config['DoF'] * tf.ones((config['K'],), dtype=tf.float32)
                DoF = variable_on_device('DoF_k', shape=None, initializer=DoF, trainable=False, device=param_device)

                alpha_k = variable_on_device('alpha_k', shape=None, initializer=theta[0], trainable=False,
                                             device=param_device)

            # init inference GMM parameters
            phi_gmm = svae.init_recognition_params(theta, config['K'], seed=config['seed'],
                                                   param_device=param_device)

            # only keep dirichlet prior, as other parameters will be point estimates
            gmm_prior = gmm_prior[0]
            theta = (alpha_k, mu_k, L_k, DoF)

        else:
            # init model-GMM
            gmm_prior, theta = svae.init_mm(config['K'], config['L'], seed=config['seed'], param_device=param_device)

            # init recognition-GMM
            phi_gmm = svae.init_recognition_params(theta, config['K'], seed=config['seed'], param_device=param_device)

        # init lists for collecting tower outputs
        tower_grads = []
        tower_x_samps = []
        tower_r_nk = []
        tower_elbo = []
        tower_neg_rec_err = []
        tower_regularizer = []
        tower_numerator = []
        tower_denominator = []
        tower_mean_rec = []
        tower_out2_rec = []

        # build computation graph (distributed on multiple GPUs if required)
        with tf.variable_scope(tf.get_variable_scope()) as v_scope:
            for gpu_id in range(nb_gpu):
                with tf.device('/gpu:%d' % gpu_id):
                    with tf.name_scope('tower_%d' % gpu_id):

                        # get current tower's part of the minibatch
                        if nb_gpu > 1:
                            y_tr_tower = y_tr[gpu_id]
                        else:
                            y_tr_tower = y_tr

                        # build model, using parameters saved on param_device
                        (y_k_rec, y_enc, x_k_samples, x_samples,
                         log_z_given_y_phi,
                         phi_gmm, phi_tilde) = svae.inference(y_tr_tower, phi_gmm, encoder_layers, decoder_layers,
                                                              nb_samples, param_device=param_device,
                                                              seed=config['seed'])

                        # share model parameters across GPUs
                        v_scope.reuse_variables()

                        # collect local variables for updating PGM parameter theta
                        tower_x_samps.append(x_samples)
                        tower_r_nk.append(log_z_given_y_phi)

                        # compute elbo
                        if 'smm' in config['method']:
                            elbo, details = svae.compute_elbo_smm(y_tr_tower, y_k_rec, theta, phi_tilde,
                                                                  x_k_samples, log_z_given_y_phi,
                                                                  decoder_type=decoder_type)
                        else:
                            elbo, details = svae.compute_elbo(y_tr_tower, y_k_rec, theta, phi_tilde,
                                                              x_k_samples, log_z_given_y_phi,
                                                              decoder_type=decoder_type)

                        # compute gradients for this tower
                        grads_and_vars = opt.compute_gradients(-elbo, gate_gradients=0)
                        tower_grads.append(grads_and_vars)

                        # save values computed in this tower
                        y_k_mean_rec, out2_rec = y_k_rec  # out2 are either bernoulli logits or gaussian variances
                        tower_mean_rec.append(y_k_mean_rec)
                        tower_out2_rec.append(out2_rec)
                        tower_elbo.append(elbo)
                        # for debugging...
                        tower_neg_rec_err.append(details[0])
                        tower_numerator.append(details[1])
                        tower_denominator.append(details[2])
                        tower_regularizer.append(details[3])

        # collect local variables from all towers
        log_z_given_y_phi = tf.concat(tower_r_nk, axis=0)
        x_samples = tf.concat(tower_x_samps, axis=0)

        # update GMM on param_device
        with tf.name_scope('GMM_update'):
            with tf.device(param_device):
                if 'smm' in config['method']:
                    # only alpha is updated...
                    alpha_star = svae.m_step_smm(smm_prior=gmm_prior, r_nk=tf.exp(log_z_given_y_phi))
                    update_theta = svae.update_gmm_params([theta[0]], [alpha_star], lrcvi)
                else:
                    theta_star = svae.m_step(gmm_prior=gmm_prior, x_samples=x_samples,
                                             r_nk=tf.exp(log_z_given_y_phi))
                    update_theta = svae.update_gmm_params(theta, theta_star, lrcvi)

        # update deterministic parameters
        with tf.name_scope('training'):
            grads_and_vars = average_gradients(tower_grads)
            update_deterministic = opt.apply_gradients(grads_and_vars, global_step=global_step)

        training_step = tf.group(update_theta, update_deterministic, name='training_ops')

        # use trained model for test prediction
        with tf.name_scope('test_performance'), tf.device(meas_device):
            tf.get_variable_scope().reuse_variables()
            (y_k_te_rec, y_te_enc, x_k_te_samples, x_te_samples,
             log_r_nk_te, _, _) = svae.inference(y_te, phi_gmm, encoder_layers, decoder_layers, nb_samples_te,
                                                 seed=config['seed'], name='test_inference')
            y_k_te_mean_rec, out2_te_rec = y_k_te_rec
            with tf.name_scope('perf_measures'):
                # test performance
                mse_te = weighted_mse(y_te_01, y_k_te_mean_rec, tf.exp(log_r_nk_te))
                if decoder_type == 'bernoulli':
                    loli_te = bernoulli_logprob(y_te, out2_te_rec, log_r_nk_te)
                else:
                    loli_te = diagonal_gaussian_logprob(y_te, y_k_te_mean_rec, out2_te_rec, log_r_nk_te)
                tf.summary.scalar('mse_te', mse_te)
                tf.summary.scalar('loli_te', loli_te)
                if lbl_te is not None:
                    entr_te, prty_te = purity(tf.exp(log_r_nk_te), lbl_te)
                    tf.summary.scalar('entropy_te', entr_te)
                    tf.summary.scalar('purity_te', prty_te)

                # training performance
                y_mean_rec = tf.concat(tower_mean_rec, axis=0)
                out2_rec = tf.concat(tower_out2_rec, axis=0)
                y_tr_coll = tf.concat(y_tr, axis=0)  # collect training batch
                mse_tr = weighted_mse(y_tr_01, y_mean_rec, tf.exp(log_z_given_y_phi))
                if decoder_type == 'bernoulli':
                    loli_tr = bernoulli_logprob(y_tr_coll, out2_rec, log_z_given_y_phi)
                else:
                    loli_tr = diagonal_gaussian_logprob(y_tr_coll, y_mean_rec, out2_rec, log_z_given_y_phi)
                tf.summary.scalar('mse_tr', mse_tr)
                tf.summary.scalar('loli_tr', loli_tr)
                if lbl_tr is not None:
                    entr_tr, prty_tr = purity(tf.exp(log_z_given_y_phi), lbl_tr)
                    tf.summary.scalar('entropy_tr', entr_tr)
                    tf.summary.scalar('purity_tr', prty_tr)

        # useful values for tensorboard and plotting
        with tf.name_scope('plotting_prep'):
            if 'smm' in config['method']:
                mu, sigma = svae.unpack_smm(theta[1:3])
            else:
                beta_k, m_k, C_k, v_k = niw.natural_to_standard(theta[1], theta[2], theta[3], theta[4])
                mu, sigma = niw.expected_values((beta_k, m_k, C_k, v_k))
            alpha_k = dirichlet.natural_to_standard(theta[0])
            expected_log_pi = dirichlet.expected_log_pi(alpha_k)
            pi_theta = tf.exp(expected_log_pi)
            theta_plot = mu, sigma, pi_theta
            q_z_given_y_phi = tf.exp(log_z_given_y_phi)
            neg_normed_elbo = -tf.divide(tf.reduce_sum(tower_elbo), size_minibatch)

            tf.summary.scalar('elbo/elbo_normed', neg_normed_elbo)
            tf.summary.scalar('elbo/neg_rec_err', tf.divide(tf.reduce_sum(tower_neg_rec_err), size_minibatch))
            tf.summary.scalar('elbo/regularizer', tf.divide(tf.reduce_sum(tower_regularizer), size_minibatch))
            tf.summary.scalar('elbo/regularizer/log_numerator', tf.reduce_sum(tower_numerator))
            tf.summary.scalar('elbo/regularizer/log_denominator', tf.reduce_sum(tower_denominator))

            phi_gmm_unpacked = svae.unpack_recognition_gmm(phi_gmm)
            phi_gmm_plot = gaussian.natural_to_standard(*phi_gmm_unpacked[:2])

            tf.summary.histogram('mixture_coefficients', pi_theta)  # keep track of component weights
            tf.summary.tensor_summary('cluster_means', mu)
            tf.summary.tensor_summary('cluster_covs', sigma)
            tf.summary.tensor_summary('cluster_weights', pi_theta)

            # for plotting...
            clustering = tf.argmax(log_r_nk_te, axis=1)  # most likely cluster allocation
            N, D = y_te.shape
            # get the first reconstruction sample of the most likely cluster... (N, K, S, D) -> (N, D)
            with tf.name_scope('prepare_indices'):
                n_idx = tf.constant(np.arange(int(N)).reshape(-1, 1), dtype=tf.int64, name='n_idx')
                k_idx = tf.reshape(clustering, (-1, 1), name='k_idx')
                s_idx = tf.constant(np.zeros(int(N)).reshape(-1, 1), dtype=tf.int64, name='s_idx')
                d_idx = tf.constant(np.arange(int(D))[None, :, None], dtype=tf.int64, name='d_idx')
                nks_idx = tf.concat([n_idx, k_idx, s_idx], axis=1, name='nks_idx')
                nksd_idx = tf.concat([tf.tile(tf.expand_dims(nks_idx, 1), (1, int(D), 1)),
                                      tf.tile(d_idx, (int(N), 1, 1))], axis=2, name='idx')  # shape (N, D, 4)
            y_te_mean_rec = tf.gather_nd(y_k_te_mean_rec, nksd_idx)

        # init tensorboard
        perf_summaries = tf.summary.merge_all()  # these summaries will be saved regularly
        log_id = generate_log_id(config)
        log_path = log_dir + '/' + log_id
        print(log_path)
        if not os.path.exists(log_path):
            os.mkdir(log_path)
        summary_writer = tf.summary.FileWriter(log_path, graph=tf.get_default_graph())

        # init model saver to store trained variables (make sure that old ckpnts are not deleted)
        model_saver = tf.train.Saver(max_to_keep=nb_iters//checkpoint_freq + 2)

        # compute imputation error (this will be done less regularly than other performance measurements)
        with tf.name_scope('test_imputation'), tf.device(meas_device):
            # mask is random, but constant for entire run.
            missing_data_mask = generate_missing_data_mask(y_te, ratio_missing_data, seed=config['seed'])

            # define imputation graph for SVAE
            def impute(y_perturbed):
                with tf.name_scope('missing_data_imputation'):
                    tf.get_variable_scope().reuse_variables()
                    ((y_k_mean_imp, out2_imp), _, _, _, log_r_nk_imp, _, _) = \
                        svae.inference(y_perturbed, phi_gmm, encoder_layers, decoder_layers, nb_samples_te,
                                       seed=config['seed'], name='test_inference')
                    return y_k_mean_imp, out2_imp, log_r_nk_imp


            # impute missing values
            imp_mse, imp_lopr = imputation_losses(y_te, missing_data_mask, impute, nb_samples_pert,
                                                  nb_samples_te, decoder_type=decoder_type, seed=config['seed'])

            imp_smry_mse = tf.summary.scalar('imp_mse', imp_mse)
            imp_smry_lopr = tf.summary.scalar('imp_logprob', imp_lopr)

            imp_summaries = [imp_smry_mse, imp_smry_lopr]

            # save missing_data_mask (add two axes for batch size and channel)
            md_mask_summary = tf.summary.image('missing_data_mask',
                                               tf.expand_dims(tf.expand_dims(tf.to_float(missing_data_mask), 0), 3))

        # save some images in summary to look at them (and their reconstruction) in tensorboard
        if config['dataset'] in ['mnist', 'mnist-small', 'fashion']:
            with tf.name_scope('sample_recs'):
                nb_rec_samps = 6
                # use same test sample for visualising reconstructions throughout training
                y_te_cnst_samp = tf.Variable(y_te[:nb_rec_samps, :], trainable=False, name='y_te_samp_fixed')
                y_rec, y_cl = svae.predict(y_te_cnst_samp, phi_gmm, encoder_layers, decoder_layers, seed=0)

                smp_te_true = tf.summary.image('test_samps',
                                               tf.reshape(y_te_cnst_samp, (nb_rec_samps, 28, 28, 1)),
                                               max_outputs=nb_rec_samps)
                smp_te_rec = tf.summary.image('test_rec_samps', tf.reshape(y_rec, (nb_rec_samps, 28, 28, 1)),
                                              max_outputs=nb_rec_samps)

        # create session, init variables and start input queue threads
        sess_config = tf.ConfigProto(allow_soft_placement=True, log_device_placement=verbose)
        sess = tf.Session(config=sess_config)
        init = tf.group(tf.global_variables_initializer(), tf.local_variables_initializer())
        coord = tf.train.Coordinator()
        threads = tf.train.start_queue_runners(sess=sess, coord=coord)
        sess.run(init)

        # init performance measurement arrays (used for plotting current training performance)
        nb_measurements = int(nb_iters / measurement_freq)
        elbo_meas = np.zeros(nb_measurements)
        debug_meas = np.zeros((nb_measurements, 4))
        perf_meas_iters = (np.arange(nb_measurements)) * measurement_freq

        # make test set plottable...
        y_te_np = sess.run(y_te_01)

        # save missing_data_mask
        summary_writer.add_summary(sess.run(md_mask_summary), 0)

        if config['dataset'] in ['mnist', 'mnist-small', 'fashion']:
            summary_writer.add_summary(sess.run(smp_te_true), 0)
        mu_dbg, sigma_dbg, pi, L_dbg = svae.unpack_recognition_gmm_debug(phi_gmm)

        start_time = time.time()

        # train
        try:
            for i in range(nb_iters):

                # periodically save tf-checkpoint and execution stats
                if i % checkpoint_freq == 0 or i == nb_iters - 1:
                    run_options = tf.RunOptions(trace_level=tf.RunOptions.FULL_TRACE)
                    run_metadata = tf.RunMetadata()
                    _, neg_elbo, dtl = sess.run([training_step, neg_normed_elbo, details],
                                                options=run_options,
                                                run_metadata=run_metadata)
                    summary_writer.add_run_metadata(run_metadata, 'step%d' % i)
                    model_saver.save(sess, log_path + '/checkpoint', global_step=i)
                    # _, neg_elbo, dtl = sess.run([training_step, neg_normed_elbo, details])  # todo
                else:
                    _, neg_elbo, dtl = sess.run([training_step, neg_normed_elbo, details])

                # evaluate performance (i.e. run performance summaries)
                if i % measurement_freq == 0 or i == nb_iters - 1 or i == 1:
                    summaries = sess.run(perf_summaries)
                    measurement_iter = int(i / measurement_freq)
                    elbo_meas[measurement_iter] = neg_elbo
                    debug_meas[measurement_iter, :] = np.squeeze(dtl)
                    print('Iteration %5d\t\t%.4fsec\t\t%.4f' % (i, time.time() - start_time, neg_elbo))
                    summary_writer.add_summary(summaries, global_step=i)

                # evaluate imputation performance
                if i % imputation_freq == 0 or i == nb_iters - 1 or i == 1:
                    imp_sum_evaluated = sess.run(imp_summaries)
                    for summary in imp_sum_evaluated:
                        summary_writer.add_summary(summary, global_step=i)

                # update plot
                if i % plot_freq == 0 or i == nb_iters - 1 or i == 1:
                    mean_cov_mc, x_samps, r_nk, y_te_rec_np, cluster_alloc = sess.run([
                        theta_plot, x_samples, q_z_given_y_phi, y_te_mean_rec, clustering])
                    plt = svae_dashboard(i, y_te_np, y_te_rec_np, x_samps, r_nk, cluster_alloc, mean_cov_mc,
                                         size_minibatch, perf_meas_iters, measurement_freq, elbo_meas,
                                         debug_meas, None)
                    plt.tight_layout()
                    plt.savefig(log_dir + '/' + log_id + '.png')

                    # save sample reconstructions
                    if config['dataset'] in ['mnist', 'mnist-small', 'fashion']:
                        summary_writer.add_summary(sess.run(smp_te_rec), global_step=i)

        finally:  # always flush summaries and close session
            print("Iteration %i; Done with experiment\n%s\n\n" % (i, str(config)))
            summary_writer.flush()
            summary_writer.close()
            sess.close()