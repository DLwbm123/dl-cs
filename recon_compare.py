"""Compare multiple reconstruction models"""
import os
import numpy as np
import argparse
import imageio
from matplotlib import pyplot
import sigpy.mri
import recon_run

from utils import mri
from utils import fftc
from utils import cfl
from utils import metrics
import utils.logging

logger = utils.logging.logger


def compute_metrics(ref, x):
    psnr = metrics.compute_psnr(ref.copy(), x.copy())
    nrmse = metrics.compute_nrmse(ref.copy(), x.copy())

    ssim_total = 0
    for i in range(ref.shape[-1]):
        ssim_i = metrics.compute_ssim(
            ref[:, :, :, i].copy(), x[:, :, :, i].copy(), sos_axis=0)
        ssim_total += ssim_i
    ssim = ssim_total / ref.shape[-1]
    return {'psnr': psnr, 'nrmse': nrmse, 'ssim': ssim}


def write_views_png(filebase, image):
    """Writes different views as png"""
    image_out = image / (np.max(np.abs(image)) * 0.9) * 255
    imageio.imwrite(
        filebase + '_sag.png', np.uint8(image_out[image_out.shape[0]//2, :, :]))
    imageio.imwrite(
        filebase + '_cor.png', np.uint8(image_out[:, image_out.shape[1]//2, :]))
    imageio.imwrite(
        filebase + '_ax.png', np.uint8(image_out[:, :, image_out.shape[2]//2]))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Run inference and comparison')
    parser.add_argument('model_root_dir', action='store',
                        help='Location of trained model')
    parser.add_argument('kspace_truth', action='store',
                        help='CFL file of kspace input data')
    parser.add_argument('output_dir', action='store',
                        help='Output dir')
    parser.add_argument('--sensemap', default=None,
                        help='Insert sensemap as CFL')
    parser.add_argument('--device', default='0',
                        help='GPU device to use')
    parser.add_argument('--batch_size', default=1, type=int,
                        help='Batch size for inference')
    parser.add_argument('--verbose', action='store_true',
                        help='Verbose printing (default: False)')
    parser.add_argument('--plot', action='store_true',
                        help='Plotting for debugging (default: False)')
    parser.add_argument('--logfile', default=None, help='Logging to file')
    args = parser.parse_args()

    log_level = utils.logging.logging.INFO if args.verbose else utils.logging.logging.WARNING
    logger.setLevel(log_level)
    if args.logfile is not None:
        logger.info('Writing log {}...'.format(args.logfile))
        file_handler = utils.logging.logging.FileHandler(args.logfile)
        file_handler.setFormatter(utils.logging.logging.Formatter(
            utils.logging.logging.BASIC_FORMAT, None))
        logger.addHandler(file_handler)

    os.environ['CUDA_VISIBLE_DEVICES'] = args.device
    logger.info('Using GPU device {}...'.format(args.device))

    logger.info('Loading k-space data from {}...'.format(args.kspace_truth))
    kspace_truth = np.load(args.kspace_truth)
    num_channels = kspace_truth.shape[0]
    shape_z = kspace_truth.shape[1]
    shape_y = kspace_truth.shape[2]
    shape_x = kspace_truth.shape[3]

    if not os.path.exists(args.output_dir):
        os.makedirs(args.output_dir)

    random_seed = 1000
    mask_accel = 12
    mask_calib = 20

    logger.info('Generating and applying sampling mask (R={})...'.format(mask_accel))
    mask = sigpy.mri.poisson(
        [shape_z, shape_y], mask_accel, calib=[mask_calib]*2, seed=random_seed)
    file_png = os.path.join(args.output_dir, 'mask.png')
    logger.info('  Writing mask png to {}...'.format(file_png))
    imageio.imwrite(file_png, np.uint8(np.abs(mask) * 255))
    logger.info('  Applying mask...')
    mask = np.reshape(mask, [1, shape_z, shape_y, 1])
    kspace_input = mask * kspace_truth
    kspace_input = kspace_input.astype(np.complex64)
    file_input = os.path.join(args.output_dir, 'kspace_input.npy')
    logger.info('  Writing input data to {}...'.format(file_input))
    np.save(file_input, kspace_input)

    if args.sensemap:
        logger.info('Loading sensitivity maps from {}...'.format(args.sensemap))
        sensemap = np.load(args.sensemap)
    else:
        logger.info('Computing sensitivity maps (sigpy jsense)...')
        JsenseApp = sigpy.mri.app.JsenseRecon(kspace_input, ksp_calib_width=mask_calib)
        sensemap = JsenseApp.run()
        logger.info('')
        del JsenseApp
        sensemap = sensemap.astype(np.complex64)
        np.save(os.path.join(args.output_dir, 'sensemap.npy'), sensemap)
    # add maps dimension: (maps, channels, z, y, x)
    if sensemap.ndim != 5:
        sensemap = np.expand_dims(sensemap, axis=0)

    logger.info('Generating truth comparison...')
    image_truth = fftc.ifft3c(kspace_truth)
    image_truth_sos = mri.sumofsq(image_truth, axis=0)
    write_views_png(os.path.join(args.output_dir, 'truth'), image_truth_sos)
    if args.plot:
        pyplot.figure()
        pyplot.imshow(image_truth_sos[image_truth_sos.shape[0]//2, :, :])
        pyplot.title('Truth')
        pyplot.pause(0.1)

    logger.info('Generating input comparison...')
    image_input = fftc.ifft3c(kspace_input)
    results = compute_metrics(image_truth, image_input)
    logger.info('Input: PSNR: {}, NRMSE: {}, SSIM: {}'.format(
        results['psnr'], results['nrmse'], results['ssim']))
    image_input_sos = mri.sumofsq(image_input, axis=0)
    write_views_png(os.path.join(args.output_dir, 'input'), image_input_sos)
    if args.plot:
        pyplot.figure()
        pyplot.imshow(image_input_sos[image_input_sos.shape[0]//2, :, :])
        pyplot.title('Input')
        pyplot.pause(0.1)
    del image_input

    model_list = os.listdir(args.model_root_dir)
    outputs = {}
    for model_basename in model_list:
        logger.info('Inference using model {}...'.format(model_basename))
        model_name = os.path.join(args.model_root_dir, model_basename)
        logger.info('  Setting up model from {}...'.format(model_name))
        model = recon_run.DeepRecon(model_name, num_channels, shape_z, shape_y,
                                    batch_size=args.batch_size, log_level=log_level)

        logger.info('  Running inference...')
        kspace_output = model.run(kspace_input.copy(), sensemap)
        kspace_output = kspace_output.astype(np.complex64)
        del model

        file_out = os.path.join(args.output_dir, 'kspace_' + model_basename + '.npy')
        logger.info('  Writing results to {}...'.format(file_out))
        np.save(file_out, kspace_output)

        logger.info('  Generating output comparison...')
        image_output = fftc.ifft3c(kspace_output)
        results = compute_metrics(image_truth, image_output)
        logger.info('{}: PSNR: {}, NRMSE: {}, SSIM: {}'.format(
            model_basename, results['psnr'], results['nrmse'], results['ssim']))
        image_output_sos = mri.sumofsq(image_output, axis=0)
        write_views_png(os.path.join(args.output_dir, model_basename), image_output_sos)
        if args.plot:
            pyplot.figure()
            pyplot.imshow(image_output_sos[image_output_sos.shape[0]//2, :, :])
            pyplot.title(model_basename)
            pyplot.pause(0.1)

    logger.info('Finished')
