import os, sys, time, shutil, tempfile, datetime, pathlib, subprocess
import numpy as np
from tqdm import trange, tqdm
from urllib.parse import urlparse
from scipy.ndimage import median_filter
from skimage.measure import label, regionprops
from skimage import filters
import cv2
import torch
import fastremap
from scipy.ndimage.morphology import binary_dilation, binary_opening
#from skimage.morphology import diameter_opening
try:
    from sklearn.cluster import DBSCAN
    SKLEARN_ENABLED = True 
except:
    SKLEARN_ENABLED = False


import logging
models_logger = logging.getLogger(__name__)
models_logger.setLevel(logging.DEBUG)

from . import transforms, dynamics, utils, plot
from .core import UnetModel, assign_device, MXNET_ENABLED, parse_model_string

_MODEL_URL = 'https://www.cellpose.org/models'
_MODEL_DIR_ENV = os.environ.get("CELLPOSE_LOCAL_MODELS_PATH")
_MODEL_DIR_DEFAULT = pathlib.Path.home().joinpath('.cellpose', 'models')
MODEL_DIR = pathlib.Path(_MODEL_DIR_ENV) if _MODEL_DIR_ENV else _MODEL_DIR_DEFAULT

def model_path(model_type, model_index, use_torch):
    torch_str = 'torch' if use_torch else ''
    basename = '%s%s_%d' % (model_type, torch_str, model_index)
    return cache_model_path(basename)

def size_model_path(model_type, use_torch):
    torch_str = 'torch' if use_torch else ''
    basename = 'size_%s%s_0.npy' % (model_type, torch_str)
    return cache_model_path(basename)

def cache_model_path(basename):
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    url = f'{_MODEL_URL}/{basename}'
    cached_file = os.fspath(MODEL_DIR.joinpath(basename)) 
    if not os.path.exists(cached_file):
        models_logger.info('Downloading: "{}" to {}\n'.format(url, cached_file))
        utils.download_url_to_file(url, cached_file, progress=True)
    return cached_file

class Cellpose():
    """ main model which combines SizeModel and CellposeModel

    Parameters
    ----------

    gpu: bool (optional, default False)
        whether or not to use GPU, will check if GPU available

    model_type: str (optional, default 'cyto')
        'cyto'=cytoplasm model; 'nuclei'=nucleus model

    net_avg: bool (optional, default True)
        loads the 4 built-in networks and averages them if True, loads one network if False

    device: gpu device (optional, default None)
        where model is saved (e.g. mx.gpu() or mx.cpu()), overrides gpu input,
        recommended if you want to use a specific GPU (e.g. mx.gpu(4) or torch.cuda.device(4))

    torch: bool (optional, default True)
        run model using torch if available

    """
    def __init__(self, gpu=False, model_type='cyto', net_avg=True, device=None, torch=True, pretrained_model=None, skel=False,
                 diam_mean=None):
        super(Cellpose, self).__init__()
        if not torch:
            if not MXNET_ENABLED:
                torch = True
        self.torch = torch
        
        # assign device (GPU or CPU)
        sdevice, gpu = assign_device(self.torch, gpu)
        self.device = device if device is not None else sdevice
        self.gpu = gpu
        model_type = 'cyto' if model_type is None else model_type
        if model_type=='cyto2' and not self.torch:
            model_type='cyto'
        
        self.skel = skel


        if model_type == 'user-trained':
            assert pretrained_model is not None, "User-trained model path not specified"
            assert not net_avg, "Net average currently only supported for cyto and nuclei models"
            assert diam_mean is not None, "For user-trained model, please specify diam_mean"

            assert os.path.exists(pretrained_model), "Path of specified model does not exists"
            pretrained_size_path = pretrained_model + "_size.npy"
            self.pretrained_model = pretrained_model
            self.pretrained_size = pretrained_size_path
            self.diam_mean = diam_mean
        else:
            if diam_mean is not None:
                models_logger.warning('>>> diam_mean set to default of model {}'.format(model_type))
            self.diam_mean = 30. if model_type != 'nuclei' else 17.
            self.pretrained_model = [model_path(model_type, j, torch) for j in range(4)]
            self.pretrained_size = size_model_path(model_type, torch)


            if not net_avg:
                self.pretrained_model = self.pretrained_model[0]

        self.cp = CellposeModel(device=self.device, gpu=self.gpu,
                                pretrained_model=self.pretrained_model,
                                diam_mean=self.diam_mean, torch=self.torch, skel=self.skel)
        self.cp.model_type = model_type

        self.sz = SizeModel(device=self.device, pretrained_size=self.pretrained_size,
                            cp_model=self.cp)
        self.sz.model_type = model_type

    def eval(self, x, batch_size=8, channels=None, channel_axis=None, z_axis=None,
             invert=False, normalize=True, diameter=30., do_3D=False, anisotropy=None,
             net_avg=True, augment=False, tile=True, tile_overlap=0.1, resample=False, interp=True, cluster=False,
             flow_threshold=0.4, dist_threshold=0.0, diam_threshold=12., min_size=15, stitch_threshold=0.0, 
             rescale=None, progress=None, skel=False, verbose=False):
        """ run cellpose and get masks

        Parameters
        ----------
        x: list or array of images
            can be list of 2D/3D images, or array of 2D/3D images, or 4D image array

        batch_size: int (optional, default 8)
            number of 224x224 patches to run simultaneously on the GPU
            (can make smaller or bigger depending on GPU memory usage)

        channels: list (optional, default None)
            list of channels, either of length 2 or of length number of images by 2.
            First element of list is the channel to segment (0=grayscale, 1=red, 2=green, 3=blue).
            Second element of list is the optional nuclear channel (0=none, 1=red, 2=green, 3=blue).
            For instance, to segment grayscale images, input [0,0]. To segment images with cells
            in green and nuclei in blue, input [2,3]. To segment one grayscale image and one
            image with cells in green and nuclei in blue, input [[0,0], [2,3]].
        
        channel_axis: int (optional, default None)
            if None, channels dimension is attempted to be automatically determined

        z_axis: int (optional, default None)
            if None, z dimension is attempted to be automatically determined

        invert: bool (optional, default False)
            invert image pixel intensity before running network (if True, image is also normalized)

        normalize: bool (optional, default True)
                normalize data so 0.0=1st percentile and 1.0=99th percentile of image intensities in each channel

        diameter: float (optional, default 30.)
            if set to None, then diameter is automatically estimated if size model is loaded

        do_3D: bool (optional, default False)
            set to True to run 3D segmentation on 4D image input

        anisotropy: float (optional, default None)
            for 3D segmentation, optional rescaling factor (e.g. set to 2.0 if Z is sampled half as dense as X or Y)

        net_avg: bool (optional, default True)
            runs the 4 built-in networks and averages them if True, runs one network if False

        augment: bool (optional, default False)
            tiles image with overlapping tiles and flips overlapped regions to augment

        tile: bool (optional, default True)
            tiles image to ensure GPU/CPU memory usage limited (recommended)

        tile_overlap: float (optional, default 0.1)
            fraction of overlap of tiles when computing flows

        resample: bool (optional, default False)
            run dynamics at original image size (will be slower but create more accurate boundaries)

        interp: bool (optional, default True)
                interpolate during 2D dynamics (not available in 3D) 
                (in previous versions it was False)

        flow_threshold: float (optional, default 0.4)
            flow error threshold (all cells with errors below threshold are kept) (not used for 3D)

        dist_threshold: float (optional, default 0.0)
            cell probability threshold (all pixels with prob above threshold kept for masks)

        min_size: int (optional, default 15)
                minimum number of pixels per mask, can turn off with -1

        stitch_threshold: float (optional, default 0.0)
            if stitch_threshold>0.0 and not do_3D and equal image sizes, masks are stitched in 3D to return volume segmentation

        rescale: float (optional, default None)
            if diameter is set to None, and rescale is not None, then rescale is used instead of diameter for resizing image

        progress: pyqt progress bar (optional, default None)
            to return progress bar status to GUI

        Returns
        -------
        masks: list of 2D arrays, or single 3D array (if do_3D=True)
                labelled image, where 0=no masks; 1,2,...=mask labels

        flows: list of lists 2D arrays, or list of 3D arrays (if do_3D=True)
            flows[k][0] = XY flow in HSV 0-255
            flows[k][1] = flows at each pixel
            flows[k][2] = the cell distance trasnform

        styles: list of 1D arrays of length 256, or single 1D array (if do_3D=True)
            style vector summarizing each image, also used to estimate size of objects in image

        diams: list of diameters, or float (if do_3D=True)

        """        
        tic0 = time.time()
        channels = [0,0] if channels is None else channels # why not just make this a default in the function header?

        estimate_size = True if (diameter is None or diameter==0) else False
        
        if estimate_size and self.pretrained_size is not None and not do_3D and x[0].ndim < 4:
            tic = time.time()
            models_logger.info('~~~ ESTIMATING CELL DIAMETER(S) ~~~')
            diams, _ = self.sz.eval(x, channels=channels, channel_axis=channel_axis, invert=invert, batch_size=batch_size, 
                                    augment=augment, tile=tile, normalize=normalize)
            rescale = self.diam_mean / np.array(diams)
            diameter = None
            models_logger.info('estimated cell diameter(s) in %0.2f sec'%(time.time()-tic))
            models_logger.info('>>> diameter(s) = ')
            if isinstance(diams, list) or isinstance(diams, np.ndarray):
                diam_string = '[' + ''.join(['%0.2f, '%d for d in diams]) + ']'
            else:
                diam_string = '[ %0.2f ]'%diams
            models_logger.info(diam_string)
        elif estimate_size:
            if self.pretrained_size is None:
                reason = 'no pretrained size model specified in model Cellpose'
            else:
                reason = 'does not work on non-2D images'
            models_logger.warning(f'could not estimate diameter, {reason}')
            diams = self.diam_mean 
        else:
            diams = diameter

        tic = time.time()
        models_logger.info('~~~ FINDING MASKS ~~~')
        masks, flows, styles = self.cp.eval(x, 
                                            batch_size=batch_size, 
                                            invert=invert, 
                                            normalize=normalize,
                                            diameter=diameter,
                                            rescale=rescale, 
                                            anisotropy=anisotropy, 
                                            channels=channels,
                                            channel_axis=channel_axis, 
                                            z_axis=z_axis,
                                            augment=augment, 
                                            tile=tile, 
                                            do_3D=do_3D, 
                                            net_avg=net_avg, 
                                            progress=progress,
                                            tile_overlap=tile_overlap,
                                            resample=resample,
                                            interp=interp,
                                            cluster=cluster,
                                            flow_threshold=flow_threshold, 
                                            dist_threshold=dist_threshold,
                                            diam_threshold=diam_threshold,
                                            min_size=min_size, 
                                            stitch_threshold=stitch_threshold,
                                            skel=skel,
                                            verbose=verbose)
        models_logger.info('>>>> TOTAL TIME %0.2f sec'%(time.time()-tic0))
    
        return masks, flows, styles, diams

class CellposeModel(UnetModel):
    """

    Parameters
    -------------------

    gpu: bool (optional, default False)
        whether or not to save model to GPU, will check if GPU available
        
    pretrained_model: str or list of strings (optional, default False)
        path to pretrained cellpose model(s), if None or False, no model loaded
        
    model_type: str (optional, default None)
        'cyto'=cytoplasm model; 'nuclei'=nucleus model; if None, pretrained_model used
        
    net_avg: bool (optional, default True)
        loads the 4 built-in networks and averages them if True, loads one network if False
        
    torch: bool (optional, default True)
        use torch nn rather than mxnet
        
    diam_mean: float (optional, default 27.)
        mean 'diameter', 27. is built in value for 'cyto' model
        
    device: mxnet device (optional, default None)
        where model is saved (mx.gpu() or mx.cpu()), overrides gpu input,
        recommended if you want to use a specific GPU (e.g. mx.gpu(4))
        
    model_dir: str (optional, default None)
        overwrite the built in model directory where cellpose looks for models
    
    skel: use skeletonized flow field model (optional, default False)

    """
    
    # still need to put the skel model trained on cellpose data into the right folder with the right name with the size model 
    def __init__(self, gpu=False, pretrained_model=False, 
                    model_type=None, net_avg=True, torch=True,
                    diam_mean=30., device=None,
                    residual_on=True, style_on=True, concatenation=False,
                    nchan=2, nclasses=3, skel=False):
        if not torch:
            if not MXNET_ENABLED:
                torch = True
        self.torch = torch
        if isinstance(pretrained_model, np.ndarray):
            pretrained_model = list(pretrained_model)
        elif isinstance(pretrained_model, str):
            pretrained_model = [pretrained_model]
            
        self.skel = skel        
        self.nclasses = nclasses 
        
        if model_type is not None or (pretrained_model and not os.path.exists(pretrained_model[0])):
            pretrained_model_string = model_type 
            if (pretrained_model_string !='cyto' 
                and pretrained_model_string !='nuclei' 
                and pretrained_model_string != 'cyto2'
                and pretrained_model_string !='skel') or pretrained_model_string is None: # plan to have a built-in skel model
                pretrained_model_string = 'cyto'
            pretrained_model = None 
            if (pretrained_model and not os.path.exists(pretrained_model[0])):
                models_logger.warning('pretrained model has incorrect path')
            models_logger.info(f'>>{pretrained_model_string}<< model set to be used')

            # TODO: check
            diam_mean = 30. if pretrained_model_string!='nuclei' else 17. # cyto2 still uses 30, right?
            
            pretrained_model = [model_path(pretrained_model_string, j, torch) for j in range(4)]
            pretrained_model = pretrained_model[0] if not net_avg else pretrained_model 
            residual_on, style_on, concatenation = True, True, False
        else:
            if pretrained_model:
                params = parse_model_string(pretrained_model[0])
                if params is not None:
                    residual_on, style_on, concatenation = params #no more nclasses here, as it was hard-coded at 3, now defaults to 3... 
                    # need to include it it the model name or extract it from the model itseld
                
        # initialize network
        super().__init__(gpu=gpu, pretrained_model=False,
                         diam_mean=diam_mean, net_avg=net_avg, device=device,
                         residual_on=residual_on, style_on=style_on, concatenation=concatenation,
                         nclasses=nclasses, torch=torch, nchan=nchan)

        self.unet = False
        self.pretrained_model = pretrained_model
        if self.pretrained_model and len(self.pretrained_model)==1:
            self.net.load_model(self.pretrained_model[0], cpu=(not self.gpu))
        ostr = ['off', 'on']
        self.net_type = 'cellpose_residual_{}_style_{}_concatenation_{}'.format(ostr[residual_on],
                                                                                ostr[style_on],
                                                                                ostr[concatenation])
    
    def eval(self, x, batch_size=8, channels=None, channel_axis=None, 
             z_axis=None, normalize=True, invert=False, 
             rescale=None, diameter=None, do_3D=False, anisotropy=None, net_avg=True, 
             augment=False, tile=True, tile_overlap=0.1,
             resample=False, interp=True, cluster=False,
             flow_threshold=0.4, dist_threshold=0.0, diam_threshold=12.,
             compute_masks=True, min_size=15, stitch_threshold=0.0, progress=None, skel=False, 
             calc_trace=False, verbose=False):
        """
            segment list of images x, or 4D array - Z x nchan x Y x X

            Parameters
            ----------
            x: list or array of images
                can be list of 2D/3D/4D images, or array of 2D/3D/4D images

            batch_size: int (optional, default 8)
                number of 224x224 patches to run simultaneously on the GPU
                (can make smaller or bigger depending on GPU memory usage)

            channels: list (optional, default None)
                list of channels, either of length 2 or of length number of images by 2.
                First element of list is the channel to segment (0=grayscale, 1=red, 2=green, 3=blue).
                Second element of list is the optional nuclear channel (0=none, 1=red, 2=green, 3=blue).
                For instance, to segment grayscale images, input [0,0]. To segment images with cells
                in green and nuclei in blue, input [2,3]. To segment one grayscale image and one
                image with cells in green and nuclei in blue, input [[0,0], [2,3]].

            channel_axis: int (optional, default None)
                if None, channels dimension is attempted to be automatically determined

            z_axis: int (optional, default None)
                if None, z dimension is attempted to be automatically determined

            normalize: bool (default, True)
                normalize data so 0.0=1st percentile and 1.0=99th percentile of image intensities in each channel

            invert: bool (optional, default False)
                invert image pixel intensity before running network

            rescale: float (optional, default None)
                resize factor for each image, if None, set to 1.0

            diameter: float (optional, default None)
                diameter for each image (only used if rescale is None), 
                if diameter is None, set to diam_mean

            do_3D: bool (optional, default False)
                set to True to run 3D segmentation on 4D image input

            anisotropy: float (optional, default None)
                for 3D segmentation, optional rescaling factor (e.g. set to 2.0 if Z is sampled half as dense as X or Y)

            net_avg: bool (optional, default True)
                runs the 4 built-in networks and averages them if True, runs one network if False

            augment: bool (optional, default False)
                tiles image with overlapping tiles and flips overlapped regions to augment

            tile: bool (optional, default True)
                tiles image to ensure GPU/CPU memory usage limited (recommended)

            tile_overlap: float (optional, default 0.1)
                fraction of overlap of tiles when computing flows

            resample: bool (optional, default False)
                run dynamics at original image size (will be slower but create more accurate boundaries)

            interp: bool (optional, default True)
                interpolate during 2D dynamics (not available in 3D) 
                (in previous versions it was False)

            flow_threshold: float (optional, default 0.4)
                flow error threshold (all cells with errors below threshold are kept) (not used for 3D)

            dist_threshold: float (optional, default 0.0)
                cell distance field threshold (all pixels with dist above threshold kept for masks)
                (used to be cell probability for older models)

            compute_masks: bool (optional, default True)
                Whether or not to compute dynamics and return masks.
                This is set to False when retrieving the styles for the size model.

            min_size: int (optional, default 15)
                minimum number of pixels per mask, can turn off with -1

            stitch_threshold: float (optional, default 0.0)
                if stitch_threshold>0.0 and not do_3D, masks are stitched in 3D to return volume segmentation

            progress: pyqt progress bar (optional, default None)
                to return progress bar status to GUI

            Returns
            -------
            masks: list of 2D arrays, or single 3D array (if do_3D=True)
                labelled image, where 0=no masks; 1,2,...=mask labels

            flows: list of lists 2D arrays, or list of 3D arrays (if do_3D=True)
                flows[k][0] = XY flow in HSV 0-255
                flows[k][1] = flows at each pixel
                flows[k][2] = the cell distance trasnform 

            styles: list of 1D arrays of length 64, or single 1D array (if do_3D=True)
                style vector summarizing each image, also used to estimate size of objects in image

        """
        if verbose:
            models_logger.info('Evaluating with skel %d, cluster %d, flow_threshold %f'%(skel,cluster,flow_threshold))
        
        
        if isinstance(x, list) or x.squeeze().ndim==5:
            masks, styles, flows = [], [], []
            tqdm_out = utils.TqdmToLogger(models_logger, level=logging.INFO)
            nimg = len(x)
            iterator = trange(nimg, file=tqdm_out) if nimg>1 else range(nimg)
            for i in iterator:
                maski, stylei, flowi = self.eval(x[i], 
                                                 batch_size=batch_size, 
                                                 channels=channels[i] if (len(channels)==len(x) and 
                                                                          (isinstance(channels[i], list) and isinstance(channels[i], np.ndarray)) and 
                                                                          len(channels[i])==2) else channels, 
                                                 channel_axis=channel_axis, 
                                                 z_axis=z_axis, 
                                                 normalize=normalize, 
                                                 invert=invert, 
                                                 rescale=rescale[i] if isinstance(rescale, list) or isinstance(rescale, np.ndarray) else rescale,
                                                 diameter=diameter[i] if isinstance(diameter, list) or isinstance(diameter, np.ndarray) else diameter, 
                                                 do_3D=do_3D, 
                                                 anisotropy=anisotropy, 
                                                 net_avg=net_avg, 
                                                 augment=augment, 
                                                 tile=tile, 
                                                 tile_overlap=tile_overlap,
                                                 resample=resample, 
                                                 interp=interp,
                                                 cluster=cluster,
                                                 flow_threshold=flow_threshold, 
                                                 dist_threshold=dist_threshold, 
                                                 diam_threshold=diam_threshold,
                                                 compute_masks=compute_masks, 
                                                 min_size=min_size, 
                                                 stitch_threshold=stitch_threshold, 
                                                 progress=progress,
                                                 skel=skel,
                                                 calc_trace=calc_trace, 
                                                 verbose=verbose)
                masks.append(maski)
                flows.append(flowi)
                styles.append(stylei)
            return masks, styles, flows 
        
        else:
            x = transforms.convert_image(x, channels, channel_axis=channel_axis, z_axis=z_axis,
                                         do_3D=(do_3D or stitch_threshold>0), normalize=False, invert=False, nchan=self.nchan, skel=skel)
            if x.ndim < 4:
                x = x[np.newaxis,...]
            self.batch_size = batch_size
            rescale = self.diam_mean / diameter if (rescale is None and (diameter is not None and diameter>0)) else rescale
            rescale = 1.0 if rescale is None else rescale
            
            if isinstance(self.pretrained_model, list) and not net_avg:
                self.net.load_model(self.pretrained_model[0], cpu=(not self.gpu))
                if not self.torch:
                    self.net.collect_params().grad_req = 'null'

            masks, styles, dP, dist, p, bd = self._run_cp(x, 
                                                          compute_masks=compute_masks,
                                                          normalize=normalize,
                                                          invert=invert,
                                                          rescale=rescale, 
                                                          net_avg=net_avg, 
                                                          resample=resample,
                                                          augment=augment, 
                                                          tile=tile, 
                                                          tile_overlap=tile_overlap,
                                                          dist_threshold=dist_threshold, 
                                                          diam_threshold=diam_threshold,
                                                          flow_threshold=flow_threshold,
                                                          interp=interp,
                                                          cluster=cluster,
                                                          min_size=min_size, 
                                                          do_3D=do_3D, 
                                                          anisotropy=anisotropy,
                                                          stitch_threshold=stitch_threshold,
                                                          skel=skel,
                                                          calc_trace=calc_trace,
                                                          verbose=verbose)
            flows = [plot.dx_to_circ(dP), dP, dist, p, bd]
            
            torch.cuda.empty_cache() #attempt to clear memory
            return masks, flows, styles

    def _run_cp(self, x, compute_masks=True, normalize=True, invert=False,
                rescale=1.0, net_avg=True, resample=False,
                augment=False, tile=True, tile_overlap=0.1,
                dist_threshold=0.0, diam_threshold=12., flow_threshold=0.4, min_size=15,
                interp=False, cluster=False, anisotropy=1.0, do_3D=False, stitch_threshold=0.0,
                skel=False, calc_trace=False, verbose=False):
        tic = time.time()
        shape = x.shape
        nimg = shape[0]        
        
        if do_3D:
            img = np.asarray(x)
            if normalize or invert:
                img = transforms.normalize_img(img, invert=invert, skel=skel)
            yf, styles = self._run_3D(img, rsz=rescale, anisotropy=anisotropy, 
                                      net_avg=net_avg, augment=augment, tile=tile,
                                      tile_overlap=tile_overlap)
            dist = yf[0][-1] + yf[1][-1] + yf[2][-1] # changed in name only, no edits to 3D yet
            dP = np.stack((yf[1][0] + yf[2][0], yf[0][0] + yf[2][1], yf[0][1] + yf[1][1]),
                          axis=0) # (dZ, dY, dX)
            
            # just for compatibility below for now
            bd = np.zeros_like(dist)            
        else:
            tqdm_out = utils.TqdmToLogger(models_logger, level=logging.INFO)
            iterator = trange(nimg, file=tqdm_out) if nimg>1 else range(nimg)
            styles = np.zeros((nimg, self.nbase[-1]), np.float32)
            if resample:
                dP = np.zeros((2, nimg, shape[1], shape[2]), np.float32)
                dist = np.zeros((nimg, shape[1], shape[2]), np.float32)
                bd = np.zeros_like(dist)
            else:
                dP = np.zeros((2, nimg, int(shape[1]*rescale), int(shape[2]*rescale)), np.float32)
                dist = np.zeros((nimg, int(shape[1]*rescale), int(shape[2]*rescale)), np.float32)
                bd = np.zeros_like(dist)
                
            for i in iterator:
                img = np.asarray(x[i])
                if normalize or invert:
                    img = transforms.normalize_img(img, invert=invert, skel=skel)
                if rescale != 1.0:
                    img = transforms.resize_image(img, rsz=rescale)

                yf, style = self._run_nets(img, net_avg=net_avg,
                                           augment=augment, tile=tile,
                                           tile_overlap=tile_overlap)
                if resample:
                    yf = transforms.resize_image(yf, shape[1], shape[2])

                dist[i] = yf[:,:,2]
                dP[:, i] = yf[:,:,:2].transpose((2,0,1)) 
                if self.nclasses == 4:
                    bd[i] = yf[:,:,3]
                styles[i] = style
        
        net_time = time.time() - tic
        if nimg > 1:
            models_logger.info('network run in %2.2fs'%(net_time))

        if compute_masks:
            tic=time.time()
            niter = 200 if do_3D else (1 / rescale * 200)
            if do_3D:
                masks, p, tr = self._compute_masks(dP, dist, bd, niter=niter, dist_threshold=dist_threshold,
                                                   diam_threshold=diam_threshold,flow_threshold=flow_threshold,
                                                   interp=interp, cluster=cluster, do_3D=do_3D, min_size=min_size,
                                                   resize=None, skel=skel, calc_trace=calc_trace, verbose=verbose)
            else:
                masks = np.zeros((nimg, shape[1], shape[2]), np.uint16)
                p = np.zeros((2, nimg, shape[1], shape[2]) if not resample else dP.shape, np.uint16)
#                 p = np.zeros(dP.shape, np.uint16)

                tr = [[]]*nimg # trace may not work correctly with multiple images currently, still need to test it 
                resize = [shape[1], shape[2]] if not resample else None
                for i in iterator:
                    masks[i], p[:,i], tr[i] = self._compute_masks(dP[:,i], dist[i], bd[i], #pi mismatch 
                                                                  niter=niter, 
                                                                  dist_threshold=dist_threshold,
                                                                  flow_threshold=flow_threshold, 
                                                                  diam_threshold=diam_threshold, 
                                                                  interp=interp, cluster=cluster,
                                                                  resize=resize, 
                                                                  skel=skel, calc_trace=calc_trace, 
                                                                  verbose=verbose)
            
                if stitch_threshold > 0 and nimg > 1:
                    models_logger.info(f'stitching {nimg} planes using stitch_threshold={stitch_threshold:0.3f} to make 3D masks')
                    masks = utils.stitch3D(masks, stitch_threshold=stitch_threshold)
            
            flow_time = time.time() - tic
            if nimg > 1:
                models_logger.info('masks created in %2.2fs'%(flow_time))
        else:
            masks, p = np.zeros(0), np.zeros(0) #pass back zeros if not compute_masks
            
        return masks.squeeze(), styles.squeeze(), dP.squeeze(), dist.squeeze(), p.squeeze(), bd.squeeze()

    def _compute_masks(self, dP, dist, bd, p=None, niter=200, dist_threshold=0.0, diam_threshold=12.,
                        flow_threshold=0.4, interp=True, cluster=False, do_3D=False, 
                        min_size=15, resize=None, skel=False, calc_trace=False, verbose=False):
        """ compute masks using dynamics from dP, dist, and boundary """
        if skel:
            mask = filters.apply_hysteresis_threshold(dist, dist_threshold-1, dist_threshold) # good for thin features
        else:
            mask = dist > dist_threshold # analog to original iscell=(cellprob>cellprob_threshold)
        
        if np.any(mask): #mask at this point is a cell cluster binary map, not labels 
            if not skel: # use original algorthm 
                if verbose:
                    models_logger.info('using original mask reconstruction algorithm')
                if p is None:
                    p , inds, tr = dynamics.follow_flows(dP * mask / 5., mask=mask, niter=niter, interp=interp, 
                                                         use_gpu=self.gpu, device=self.device, skel=skel, calc_trace=calc_trace)

                else: 
                    inds,tr = [],[]
                    if verbose:
                        models_logger.info('p given')
                print(p.shape, mask.shape)
                mask = dynamics.get_masks(p, iscell=mask,flows=dP, threshold=flow_threshold if not do_3D else None, 
                                          use_gpu=self.gpu)

            else: # use new algorithm
                Ly,Lx = mask.shape
                if self.nclasses == 4:
                    dt = np.abs(dist[mask]) #abs needed if the threshold is negative
                    d = utils.dist_to_diam(dt)
                    eps = 1+1/3

                else: #backwards compatibility, doesn't help for *clusters* of thin/small cells
                    d,e = utils.diameters(mask,skel)
                    eps = np.sqrt(2)

                # save unaltered versions for later 
                dP = dP.copy()

                # The mean diameter can inform whether or not the cells are too small to form contiguous blobs.
                # My first solution was to upscale everything before Euler integration to give pixels 'room' to
                # stay together. My new solution is much better: use a clustering algorithm on the sub-pixel coordinates
                # to assign labels. It works just as well and is faster because it doesn't require increasing the 
                # number of points or taking time to upscale/downscale the data. Users can toggle cluster on manually or
                # by setting the diameter threshold higher than the average diameter of the cells. 
                if d <= diam_threshold:
                    cluster = True
                    if verbose:
                        models_logger.info('Turning on subpixel clustering for label continuity.')

                dP *= mask 
                dP = transforms.normalize_field(dP,skel=True)

                # compute the divergence
                Y, X = np.nonzero(mask)
                pad = 1
                Tx = np.zeros((Ly+2*pad)*(Lx+2*pad), np.float64)
                Tx[Y*Lx+X] = np.reshape(dP[1].copy(),Ly*Lx)[Y*Lx+X]
                Ty = np.zeros((Ly+2*pad)*(Lx+2*pad), np.float64)
                Ty[Y*Lx+X] = np.reshape(dP[0].copy(),Ly*Lx)[Y*Lx+X]

                # Rescaling by the divergence
                div = np.zeros(Ly*Lx, np.float64)
                div[Y*Lx+X]=(Ty[(Y+2)*Lx+X]+8*Ty[(Y+1)*Lx+X]-8*Ty[(Y-1)*Lx+X]-Ty[(Y-2)*Lx+X]+
                             Tx[Y*Lx+X+2]+8*Tx[Y*Lx+X+1]-8*Tx[Y*Lx+X-1]-Tx[Y*Lx+X-2])
                div = transforms.normalize99(div,skel=True)
                div.shape = (Ly,Lx)
                #add sigmoid on boundary output to help push pixels away - the final bit needed in some cases!
                # specifically, places where adjacent cell flows are too colinear and therefore had low divergence
#                 mag = div+1/(1+np.exp(-bd))
                dP *= div

                p, inds, tr = dynamics.follow_flows(dP, mask, interp=interp, use_gpu=self.gpu,
                                                    device=self.device, skel=skel, calc_trace=calc_trace)

                newinds = p[:,inds[:,0],inds[:,1]].swapaxes(0,1)
                mask = np.zeros((p.shape[1],p.shape[2]))

                # the eps parameter needs to be adjustable... maybe a function of the distance
                if cluster:
                    if verbose:
                        models_logger.info('Doing DBSCAN clustering with eps=%f'%eps)
                    db = DBSCAN(eps=eps, min_samples=3,n_jobs=8).fit(newinds)
                    labels = db.labels_
                    mask[inds[:,0],inds[:,1]] = labels+1
                else:
                    newinds = np.rint(newinds).astype(int)
                    skelmask = np.zeros_like(dist, dtype=bool)
                    skelmask[newinds[:,0],newinds[:,1]] = 1

                    #disconnect skeletons at the edge, 5 pixels in 
                    border_mask = np.zeros(skelmask.shape, dtype=bool)
                    border_px =  border_mask.copy()
                    border_mask = binary_dilation(border_mask, border_value=1, iterations=5)

                    border_px[border_mask] = skelmask[border_mask]
                    if self.nclasses == 4: #can use boundary to erase joined edge skelmasks 
                        border_px[bd>-1] = 0
                        if verbose:
                            models_logger.info('Using boundary output to split edge defects')
                    else: #otherwise do morphological opening to attempt splitting 
                        border_px = binary_opening(border_px,border_value=0,iterations=3)

                    skelmask[border_mask] = border_px[border_mask]
                    
                    LL = label(skelmask,connectivity=1) 
                    mask[inds[:,0],inds[:,1]] = LL[newinds[:,0],newinds[:,1]]

            # quality control - this got removed in recent version of cellpose??? or did I add it? 
#             if flow_threshold is not None and flow_threshold > 0 and dP is not None:
#                 mask = dynamics.remove_bad_flow_masks(mask, dP, threshold=flow_threshold, skel=skel)

            if resize is not None:
                if verbose:
                    models_logger.info(f'resizing output with resize = {resize}')
                mask = transforms.resize_image(mask, resize[0], resize[1], interpolation=cv2.INTER_NEAREST)
                Ly,Lx = mask.shape
                pi = np.zeros([2,Ly,Lx])
                for k in range(2):
                    pi[k] = cv2.resize(p[k], (Lx, Ly), interpolation=cv2.INTER_NEAREST)
                p = pi       
        else: # nothing to compute, just make it compatible
            models_logger.info('No cell pixels found.')
            p = np.zeros([2,1,1])
            tr = []
            mask = np.zeros(resize)

        # moving the cleanup to the end helps avoid some bugs arising from scaling...
        # maybe better would be to rescale the min_size and hole_size parameters to do the
        # cleanup at the prediction scale, or switch depending on which one is bigger... 
        mask = utils.fill_holes_and_remove_small_masks(mask, min_size=min_size)
        fastremap.renumber(mask,in_place=True) #convenient to guarantee non-skipped labels
        return mask, p, tr

    def loss_fn(self, lbl, y):
        """ loss function between true labels lbl and prediction y """
        if not self.skel: # original loss function 
            veci = 5. * self._to_device(lbl[:,1:])
            lbl  = self._to_device(lbl[:,0]>.5)
            loss = self.criterion(y[:,:2] , veci) 
            if self.torch:
                loss /= 2.
            loss2 = self.criterion2(y[:,2] , lbl)
            loss = loss + loss2
        
        else: #loss function for skeletonized field 
            veci = self._to_device(lbl[:,2:4]) #scaled to 5 in augmentation 
            dist = lbl[:,1] # now distance transform replaces probability
            boundary =  lbl[:,5]
            cellmask = dist>0
            w =  self._to_device(lbl[:,7])  # new smooth, boundary-emphasized weight calculated with augmentations  
            dist = self._to_device(dist)
            boundary = self._to_device(boundary)
            cellmask = self._to_device(cellmask).bool()
            flow = y[:,:2] # 0,1
            dt = y[:,2]
            bd = y[:,3]

            loss7 = 2.*self.criterion12(dt,dist,w) #weighted MSE 

            wt = torch.stack((w,w),dim=1)
            ct = torch.stack((cellmask,cellmask),dim=1) 
            loss1 = 10.*self.criterion12(flow,veci,wt)  #weighted MSE 

            loss2 = self.criterion14(flow,veci,w,cellmask) #ArcCosDotLoss
            a = 10.
            loss3 = self.criterion11(flow,veci,wt,ct)/a # DerivativeLoss
            loss8 = self.criterion11(dt.unsqueeze(1),dist.unsqueeze(1),w.unsqueeze(1),cellmask.unsqueeze(1))/a  #older models had just plain cellmask

    #         loss4 = ((self.criterion2(bd,boundary)/2.) + (self.criterion2(bd[cellmask],boundary[cellmask])))#boundary loss 
            loss4 = 2.*self.criterion2(bd,boundary)

            loss5 = 2.*self.criterion15(flow,veci,w,cellmask) # loss on norm 
    #         loss6 = self.criterion16(flow,veci,cellmask)/5. # loss on divergence, bad for normalized field, revisit on interp field?

    #         print(loss1.cpu().detach().numpy(),loss2.cpu().detach().numpy(),
    #               loss3.cpu().detach().numpy(),loss4.cpu().detach().numpy(),
    #               loss5.cpu().detach().numpy(),
    #               loss7.cpu().detach().numpy(),loss8.cpu().detach().numpy())

            loss = loss1 + loss2 + loss3 + loss4 + loss5 + loss7 + loss8
        return loss        


    def train(self, train_data, train_labels, train_files=None, 
              test_data=None, test_labels=None, test_files=None,
              channels=None, normalize=True, pretrained_model=None, 
              save_path=None, save_every=100, save_each=False,
              learning_rate=0.2, n_epochs=500, momentum=0.9, 
              weight_decay=0.00001, batch_size=8, rescale=False, skel=False):

        """ train network with images train_data 
        
            Parameters
            ------------------

            train_data: list of arrays (2D or 3D)
                images for training

            train_labels: list of arrays (2D or 3D)
                labels for train_data, where 0=no masks; 1,2,...=mask labels
                can include flows as additional images

            train_files: list of strings
                file names for images in train_data (to save flows for future runs)

            test_data: list of arrays (2D or 3D)
                images for testing

            test_labels: list of arrays (2D or 3D)
                labels for test_data, where 0=no masks; 1,2,...=mask labels; 
                can include flows as additional images
        
            test_files: list of strings
                file names for images in test_data (to save flows for future runs)

            channels: list of ints (default, None)
                channels to use for training

            normalize: bool (default, True)
                normalize data so 0.0=1st percentile and 1.0=99th percentile of image intensities in each channel

            pretrained_model: string (default, None)
                path to pretrained_model to start from, if None it is trained from scratch

            save_path: string (default, None)
                where to save trained model, if None it is not saved

            save_every: int (default, 100)
                save network every [save_every] epochs

            learning_rate: float (default, 0.2)
                learning rate for training

            n_epochs: int (default, 500)
                how many times to go through whole training set during training

            weight_decay: float (default, 0.00001)

            batch_size: int (optional, default 8)
                number of 224x224 patches to run simultaneously on the GPU
                (can make smaller or bigger depending on GPU memory usage)

            rescale: bool (default, True)
                whether or not to rescale images to diam_mean during training, 
                if True it assumes you will fit a size model after training or resize your images accordingly,
                if False it will try to train the model to be scale-invariant (works worse)

        """
        if rescale:
            models_logger.info(f'Training with rescale = {rescale:.2f}')
        train_data, train_labels, test_data, test_labels, run_test = transforms.reshape_train_test(train_data, train_labels,
                                                                                                   test_data, test_labels,
                                                                                                   channels, normalize, skel)
        # check if train_labels have flows
        train_flows = dynamics.labels_to_flows(train_labels, files=train_files, use_gpu=self.gpu, device=self.device, skel=skel)
        if run_test:
            test_flows = dynamics.labels_to_flows(test_labels, files=test_files)
        else:
            test_flows = None
        
        model_path = self._train_net(train_data, train_flows, 
                                     test_data, test_flows,
                                     pretrained_model, save_path, save_every, save_each,
                                     learning_rate, n_epochs, momentum, weight_decay, batch_size, rescale)
        self.pretrained_model = model_path
        return model_path

class SizeModel():
    """ linear regression model for determining the size of objects in image
        used to rescale before input to cp_model
        uses styles from cp_model

        Parameters
        -------------------

        cp_model: UnetModel or CellposeModel
            model from which to get styles

        device: mxnet device (optional, default mx.cpu())
            where cellpose model is saved (mx.gpu() or mx.cpu())

        pretrained_size: str
            path to pretrained size model
            
        skel: bool
            whether or not to use distance-based size metrics
            corresponding to 'skel' model 

    """
    def __init__(self, cp_model, device=None, pretrained_size=None, **kwargs):
        super(SizeModel, self).__init__(**kwargs)

        self.pretrained_size = pretrained_size
        self.cp = cp_model
        self.device = self.cp.device
        self.diam_mean = self.cp.diam_mean
        self.torch = self.cp.torch
        if pretrained_size is not None:
            self.params = np.load(self.pretrained_size, allow_pickle=True).item()
            self.diam_mean = self.params['diam_mean']
        if not hasattr(self.cp, 'pretrained_model'):
            error_message = 'no pretrained cellpose model specified, cannot compute size'
            models_logger.critical(error_message)
            raise ValueError(error_message)
        
    def eval(self, x, channels=None, channel_axis=None, 
             normalize=True, invert=False, augment=False, tile=True,
             batch_size=8, progress=None, interp=True, skel=False):
        """ use images x to produce style or use style input to predict size of objects in image

            Object size estimation is done in two steps:
            1. use a linear regression model to predict size from style in image
            2. resize image to predicted size and run CellposeModel to get output masks.
                Take the median object size of the predicted masks as the final predicted size.

            Parameters
            -------------------

            x: list or array of images
                can be list of 2D/3D images, or array of 2D/3D images

            channels: list (optional, default None)
                list of channels, either of length 2 or of length number of images by 2.
                First element of list is the channel to segment (0=grayscale, 1=red, 2=green, 3=blue).
                Second element of list is the optional nuclear channel (0=none, 1=red, 2=green, 3=blue).
                For instance, to segment grayscale images, input [0,0]. To segment images with cells
                in green and nuclei in blue, input [2,3]. To segment one grayscale image and one
                image with cells in green and nuclei in blue, input [[0,0], [2,3]].

            channel_axis: int (optional, default None)
                if None, channels dimension is attempted to be automatically determined

            normalize: bool (default, True)
                normalize data so 0.0=1st percentile and 1.0=99th percentile of image intensities in each channel

            invert: bool (optional, default False)
                invert image pixel intensity before running network

            augment: bool (optional, default False)
                tiles image with overlapping tiles and flips overlapped regions to augment

            tile: bool (optional, default True)
                tiles image to ensure GPU/CPU memory usage limited (recommended)

            progress: pyqt progress bar (optional, default None)
                to return progress bar status to GUI

            Returns
            -------
            diam: array, float
                final estimated diameters from images x or styles style after running both steps

            diam_style: array, float
                estimated diameters from style alone

        """
        
        if isinstance(x, list):
            diams, diams_style = [], []
            nimg = len(x)
            tqdm_out = utils.TqdmToLogger(models_logger, level=logging.INFO)
            iterator = trange(nimg, file=tqdm_out) if nimg>1 else range(nimg)
            for i in iterator:
                diam, diam_style = self.eval(x[i], 
                                             channels=channels[i] if (len(channels)==len(x) and 
                                                                     (isinstance(channels[i], list) and isinstance(channels[i], np.ndarray)) and 
                                                                     len(channels[i])==2) else channels,
                                             channel_axis=channel_axis, 
                                             normalize=normalize, 
                                             invert=invert,
                                             augment=augment,
                                             tile=tile,
                                             batch_size=batch_size,
                                             progress=progress,
                                             skel=skel)
                diams.append(diam)
                diams_style.append(diam_style)

            return diams, diams_style

        if x.squeeze().ndim > 3:
            models_logger.warning('image is not 2D cannot compute diameter')
            return self.diam_mean, self.diam_mean

        styles = self.cp.eval(x, 
                              channels=channels, 
                              channel_axis=channel_axis, 
                              normalize=normalize, 
                              invert=invert, 
                              augment=augment, 
                              tile=tile,
                              batch_size=batch_size, 
                              net_avg=False,
                              compute_masks=False)[-1]

        diam_style = self._size_estimation(np.array(styles))
        diam_style = self.diam_mean if (diam_style==0 or np.isnan(diam_style)) else diam_style
        
        masks = self.cp.eval(x, 
                             channels=channels, 
                             channel_axis=channel_axis, 
                             normalize=normalize, 
                             invert=invert, 
                             augment=augment, 
                             tile=tile,
                             batch_size=batch_size, 
                             net_avg=False,
                             rescale=(self.diam_mean / diam_style), 
#                              rescale =  self.diam_mean / diam_style if self.diam_mean>0 else 1, 
#                              rescale = None, 
                             diameter=None,
#                              interp=interp,
                             interp=False,
#                              flow_threshold=0,
                             skel=skel)[0]
        
        # allow backwards compatibility to older scale metric
        diam = utils.diameters(masks,skel=skel)[0]
        if hasattr(self, 'model_type') and (self.model_type=='nuclei' or self.model_type=='cyto') and not self.torch and not skel:
            diam_style /= (np.pi**0.5)/2
            diam = self.diam_mean / ((np.pi**0.5)/2) if (diam==0 or np.isnan(diam)) else diam
        else:
            diam = self.diam_mean if (diam==0 or np.isnan(diam)) else diam
        return diam, diam_style

    def _size_estimation(self, style):
        """ linear regression from style to size 
        
            sizes were estimated using "diameters" from square estimates not circles; 
            therefore a conversion factor is included (to be removed)
        
        """
        szest = np.exp(self.params['A'] @ (style - self.params['smean']).T +
                        np.log(self.diam_mean) + self.params['ymean'])
        szest = np.maximum(5., szest)
        return szest

    def train(self, train_data, train_labels,
              test_data=None, test_labels=None,
              channels=None, normalize=True, 
              learning_rate=0.2, n_epochs=10, 
              l2_regularization=1.0, batch_size=8,
              skel=False):
        """ train size model with images train_data to estimate linear model from styles to diameters
        
            Parameters
            ------------------

            train_data: list of arrays (2D or 3D)
                images for training

            train_labels: list of arrays (2D or 3D)
                labels for train_data, where 0=no masks; 1,2,...=mask labels
                can include flows as additional images

            channels: list of ints (default, None)
                channels to use for training

            normalize: bool (default, True)
                normalize data so 0.0=1st percentile and 1.0=99th percentile of image intensities in each channel

            n_epochs: int (default, 10)
                how many times to go through whole training set (taking random patches) for styles for diameter estimation

            l2_regularization: float (default, 1.0)
                regularize linear model from styles to diameters

            batch_size: int (optional, default 8)
                number of 224x224 patches to run simultaneously on the GPU
                (can make smaller or bigger depending on GPU memory usage)
        """
        batch_size /= 2 # reduce batch_size by factor of 2 to use larger tiles
        batch_size = int(max(1, batch_size))
        self.cp.batch_size = batch_size
        train_data, train_labels, test_data, test_labels, run_test = transforms.reshape_train_test(train_data, train_labels,
                                                                                                   test_data, test_labels,
                                                                                                   channels, normalize, skel)
        if isinstance(self.cp.pretrained_model, list):
            cp_model_path = self.cp.pretrained_model[0]
            self.cp.net.load_model(cp_model_path, cpu=(not self.cp.gpu))
            if not self.torch:
                self.cp.net.collect_params().grad_req = 'null'
        else:
            cp_model_path = self.cp.pretrained_model
        
        diam_train = np.array([utils.diameters(lbl,skel=skel)[0] for lbl in train_labels])
        if run_test: 
            diam_test = np.array([utils.diameters(lbl,skel=skel)[0] for lbl in test_labels])
        
        # remove images with no masks
        for i in range(len(diam_train)):
            if diam_train[i]==0.0:
                del train_data[i]
                del train_labels[i]
        if run_test:
            for i in range(len(diam_test)):
                if diam_test[i]==0.0:
                    del test_data[i]
                    del test_labels[i]

        nimg = len(train_data)
        styles = np.zeros((n_epochs*nimg, 256), np.float32)
        diams = np.zeros((n_epochs*nimg,), np.float32)
        tic = time.time()
        for iepoch in range(n_epochs):
            iall = np.arange(0,nimg,1,int)
            for ibatch in range(0,nimg,batch_size):
                inds = iall[ibatch:ibatch+batch_size]
                #using the orignal rotate and resize just because mine apparently broke the feature that
                # you could either pass in flows or masks... will eventually fix and streamline 
                imgi,lbl,scale = transforms.original_random_rotate_and_resize([train_data[i] for i in inds],
                                                                              Y=[train_labels[i].astype(np.int16) for i in inds], 
                                                                              scale_range=1, xy=(512,512)) 

                feat = self.cp.network(imgi)[1]
                styles[inds+nimg*iepoch] = feat
                diams[inds+nimg*iepoch] = np.log(diam_train[inds]) - np.log(self.diam_mean) + np.log(scale)
            del feat
            if (iepoch+1)%2==0:
                models_logger.info('ran %d epochs in %0.3f sec'%(iepoch+1, time.time()-tic))

        # create model
        smean = styles.mean(axis=0)
        X = ((styles - smean).T).copy()
        ymean = diams.mean()
        y = diams - ymean

        A = np.linalg.solve(X@X.T + l2_regularization*np.eye(X.shape[0]), X @ y)
        ypred = A @ X
        models_logger.info('train correlation: %0.4f'%np.corrcoef(y, ypred)[0,1])
            
        if run_test:
            nimg_test = len(test_data)
            styles_test = np.zeros((nimg_test, 256), np.float32)
            for i in range(nimg_test):
                styles_test[i] = self.cp._run_net(test_data[i].transpose((1,2,0)))[1]
            diam_test_pred = np.exp(A @ (styles_test - smean).T + np.log(self.diam_mean) + ymean)
            diam_test_pred = np.maximum(5., diam_test_pred)
            models_logger.info('test correlation: %0.4f'%np.corrcoef(diam_test, diam_test_pred)[0,1])

        self.pretrained_size = cp_model_path+'_size.npy'
        self.params = {'A': A, 'smean': smean, 'diam_mean': self.diam_mean, 'ymean': ymean}
        np.save(self.pretrained_size, self.params)
        models_logger.info('model saved to '+self.pretrained_size)
        return self.params
