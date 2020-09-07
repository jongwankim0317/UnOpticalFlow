import os, sys
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from structures import *
from pytorch_ssim import SSIM
import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
import pdb
import cv2
from torch.autograd import Variable

def transformerFwd(U,
                   flo,
                   out_size,
                   name='SpatialTransformerFwd'):
    """Forward Warping Layer described in 
    'Occlusion Aware Unsupervised Learning of Optical Flow by Yang Wang et al'

    Parameters
    ----------
    U : float
        The output of a convolutional net should have the
        shape [num_batch, height, width, num_channels].
    flo: float
        The optical flow used for forward warping 
        having the shape of [num_batch, height, width, 2].
    backprop: boolean
        Indicates whether to back-propagate through forward warping layer
    out_size: tuple of two ints
        The size of the output of the network (height, width)
    """

    def _repeat(x, n_repeats):
        rep = torch.ones(size=[n_repeats], dtype=torch.long).unsqueeze(1).transpose(1,0)
        x = x.view([-1,1]).mm(rep)
        return x.view([-1]).int()

    def _interpolate(im, x, y, out_size):
        # constants
        num_batch, height, width, channels = im.shape[0], im.shape[1], im.shape[2], im.shape[3]
        out_height = out_size[0]
        out_width = out_size[1]
        max_y = int(height - 1)
        max_x = int(width - 1)

        # scale indices from [-1, 1] to [0, width/height]
        x = (x + 1.0) * (width - 1.0) / 2.0
        y = (y + 1.0) * (height - 1.0) / 2.0

        # do sampling
        x0 = (torch.floor(x)).int()
        x1 = x0 + 1
        y0 = (torch.floor(y)).int()
        y1 = y0 + 1

        x0_c = torch.clamp(x0, 0, max_x)
        x1_c = torch.clamp(x1, 0, max_x)
        y0_c = torch.clamp(y0, 0, max_y)
        y1_c = torch.clamp(y1, 0, max_y)

        dim2 = width
        dim1 = width * height
        base = _repeat(torch.arange(0, num_batch) * dim1, out_height * out_width).to(im.get_device())

        base_y0 = base + y0_c * dim2
        base_y1 = base + y1_c * dim2
        idx_a = base_y0 + x0_c
        idx_b = base_y1 + x0_c
        idx_c = base_y0 + x1_c
        idx_d = base_y1 + x1_c

        # use indices to lookup pixels in the flat image and restore
        # channels dim
        im_flat = im.view([-1, channels])
        im_flat = im_flat.float()

        # and finally calculate interpolated values
        x0_f = x0.float()
        x1_f = x1.float()
        y0_f = y0.float()
        y1_f = y1.float()
        wa = ((x1_f - x) * (y1_f - y)).unsqueeze(1)
        wb = ((x1_f - x) * (y - y0_f)).unsqueeze(1)
        wc = ((x - x0_f) * (y1_f - y)).unsqueeze(1)
        wd = ((x - x0_f) * (y - y0_f)).unsqueeze(1)

        zerof = torch.zeros_like(wa)
        wa = torch.where(
            (torch.eq(x0_c, x0) & torch.eq(y0_c, y0)).unsqueeze(1), wa, zerof)
        wb = torch.where(
            (torch.eq(x0_c, x0) & torch.eq(y1_c, y1)).unsqueeze(1), wb, zerof)
        wc = torch.where(
            (torch.eq(x1_c, x1) & torch.eq(y0_c, y0)).unsqueeze(1), wc, zerof)
        wd = torch.where(
            (torch.eq(x1_c, x1) & torch.eq(y1_c, y1)).unsqueeze(1), wd, zerof)

        zeros = torch.zeros(
            size=[
                int(num_batch) * int(height) *
                int(width), int(channels)
            ],
            dtype=torch.float)
        output = zeros.to(im.get_device())
        output = output.scatter_add(dim=0, index=idx_a.long().unsqueeze(1).repeat(1,channels), src=im_flat * wa)
        output = output.scatter_add(dim=0, index=idx_b.long().unsqueeze(1).repeat(1,channels), src=im_flat * wb)
        output = output.scatter_add(dim=0, index=idx_c.long().unsqueeze(1).repeat(1,channels), src=im_flat * wc)
        output = output.scatter_add(dim=0, index=idx_d.long().unsqueeze(1).repeat(1,channels), src=im_flat * wd)

        return output

    def _meshgrid(height, width):
        # This should be equivalent to:
        x_t, y_t = np.meshgrid(np.linspace(-1, 1, width),
                                 np.linspace(-1, 1, height))
        #  ones = np.ones(np.prod(x_t.shape))
        #  grid = np.vstack([x_t.flatten(), y_t.flatten(), ones])
        return torch.from_numpy(x_t).float(), torch.from_numpy(y_t).float()

    def _transform(flo, input_dim, out_size):
        num_batch, height, width, num_channels = input_dim.shape[0:4]

        # grid of (x_t, y_t, 1), eq (1) in ref [1]
        height_f = float(height)
        width_f = float(width)
        out_height = out_size[0]
        out_width = out_size[1]
        x_s, y_s = _meshgrid(out_height, out_width)
        x_s = x_s.to(flo.get_device()).unsqueeze(0)
        x_s = x_s.repeat([num_batch, 1, 1])

        y_s = y_s.to(flo.get_device()).unsqueeze(0)
        y_s =y_s.repeat([num_batch, 1, 1])

        x_t = x_s + flo[:, :, :, 0] / ((out_width - 1.0) / 2.0)
        y_t = y_s + flo[:, :, :, 1] / ((out_height - 1.0) / 2.0)

        x_t_flat = x_t.view([-1])
        y_t_flat = y_t.view([-1])

        input_transformed = _interpolate(input_dim, x_t_flat, y_t_flat,
                                            out_size)

        output = input_transformed.view([num_batch, out_height, out_width, num_channels])
        return output

    #out_size = int(out_size)
    output = _transform(flo, U, out_size)
    return output


class Model_flow(nn.Module):
    def __init__(self, cfg):
        super(Model_flow, self).__init__()
        self.fpyramid = FeaturePyramid()
        self.pwc_model = PWC_tf()
        if cfg.mode == 'depth' or cfg.mode == 'flowposenet':
            # Stage 2 training
            for param in self.fpyramid.parameters():
                param.requires_grad = False
            for param in self.pwc_model.parameters():
                param.requires_grad = False
        
        # hyperparameters
        self.dataset = cfg.dataset
        self.num_scales = cfg.num_scales
        self.flow_consist_alpha = cfg.h_flow_consist_alpha
        self.flow_consist_beta = cfg.h_flow_consist_beta

        print("this is paper method.")


    def get_occlusion_mask_from_flow(self, tensor_size, flow):
        mask = torch.ones(tensor_size).to(flow.get_device())
        h, w = mask.shape[2], mask.shape[3]
        occ_mask = transformerFwd(mask.permute(0,2,3,1), flow.permute(0,2,3,1), out_size=[h,w]).permute(0,3,1,2)
        with torch.no_grad():
            occ_mask = torch.clamp(occ_mask, 0.0, 1.0)
        return occ_mask

    def get_flow_norm(self, flow, p=2):
        '''
        Inputs:
        flow (bs, 2, H, W)
        '''
        flow_norm = torch.norm(flow, p=p, dim=1).unsqueeze(1) + 1e-12
        return flow_norm

    def get_flow_normalization(self, flow, p=2):
        '''
        Inputs:
        flow (bs, 2, H, W)
        '''
        flow_norm = torch.norm(flow, p=p, dim=1).unsqueeze(1) + 1e-12
        flow_normalization = flow / flow_norm.repeat(1,2,1,1)
        return flow_normalization

    def get_visible_masks(self, optical_flows, optical_flows_rev):
        # get occlusion masks
        batch_size, _, img_h, img_w = optical_flows[0].shape
        img2_visible_masks, img1_visible_masks = [], []
        for s, (optical_flow, optical_flow_rev) in enumerate(zip(optical_flows, optical_flows_rev)):
            shape = [batch_size, 1, int(img_h / (2**s)), int(img_w / (2**s))]
            img2_visible_masks.append(self.get_occlusion_mask_from_flow(shape, optical_flow))
            img1_visible_masks.append(self.get_occlusion_mask_from_flow(shape, optical_flow_rev))
        return img2_visible_masks, img1_visible_masks

    def get_consistent_masks(self, optical_flows, optical_flows_rev):
        # get consist masks
        batch_size, _, img_h, img_w = optical_flows[0].shape
        img2_consis_masks, img1_consis_masks, fwd_flow_diff_pyramid, bwd_flow_diff_pyramid = [], [], [], []
        for s, (optical_flow, optical_flow_rev) in enumerate(zip(optical_flows, optical_flows_rev)):
            
            bwd2fwd_flow = warp_flow(optical_flow_rev, optical_flow)
            fwd2bwd_flow = warp_flow(optical_flow, optical_flow_rev)

            fwd_flow_diff = torch.abs(bwd2fwd_flow + optical_flow)
            fwd_flow_diff_pyramid.append(fwd_flow_diff)
            bwd_flow_diff = torch.abs(fwd2bwd_flow + optical_flow_rev)
            bwd_flow_diff_pyramid.append(bwd_flow_diff)

            # flow consistency condition
            bwd_consist_bound = torch.max(self.flow_consist_beta * self.get_flow_norm(optical_flow_rev), torch.from_numpy(np.array([self.flow_consist_alpha])).float().to(optical_flow_rev.get_device()))
            fwd_consist_bound = torch.max(self.flow_consist_beta * self.get_flow_norm(optical_flow), torch.from_numpy(np.array([self.flow_consist_alpha])).float().to(optical_flow.get_device()))
            with torch.no_grad():
                noc_masks_img2 = (self.get_flow_norm(bwd_flow_diff) < bwd_consist_bound).float()
                noc_masks_img1 = (self.get_flow_norm(fwd_flow_diff) < fwd_consist_bound).float()
                img2_consis_masks.append(noc_masks_img2)
                img1_consis_masks.append(noc_masks_img1)
        return img2_consis_masks, img1_consis_masks, fwd_flow_diff_pyramid, bwd_flow_diff_pyramid

    def generate_img_pyramid(self, img, num_pyramid):
        img_h, img_w = img.shape[2], img.shape[3]
        img_pyramid = []
        for s in range(num_pyramid):
            img_new = F.adaptive_avg_pool2d(img, [int(img_h / (2**s)), int(img_w / (2**s))]).data
            img_pyramid.append(img_new)
        return img_pyramid

    def warp_flow_pyramid(self, img_pyramid, flow_pyramid):
        img_warped_pyramid = []
        for img, flow in zip(img_pyramid, flow_pyramid):
            img_warped_pyramid.append(warp_flow(img, flow, use_mask=True))
        return img_warped_pyramid

    def compute_loss_pixel(self, img_pyramid, img_warped_pyramid, occ_mask_list):
        loss_list = []
        for scale in range(self.num_scales):
            img, img_warped, occ_mask = img_pyramid[scale], img_warped_pyramid[scale], occ_mask_list[scale]
            divider = occ_mask.mean((1,2,3))
            img_diff = torch.abs((img - img_warped)) * occ_mask.repeat(1,3,1,1)
            loss_pixel = img_diff.mean((1,2,3)) / (divider + 1e-12) # (B)
            loss_list.append(loss_pixel[:,None])
        loss = torch.cat(loss_list, 1).sum(1) # (B)
        return loss

    def compute_loss_pixel_without_mask(self, img_pyramid, img_warped_pyramid):
        loss_list = []
        for scale in range(self.num_scales):
            img, img_warped = img_pyramid[scale], img_warped_pyramid[scale]
            img_diff = torch.abs((img - img_warped))
            loss_pixel = img_diff.mean((1,2,3)) # (B)
            loss_list.append(loss_pixel[:,None])
        loss = torch.cat(loss_list, 1).sum(1) # (B)
        return loss


    def compute_loss_with_mask(self, diff_list, occ_mask_list):
        loss_list = []
        for scale in range(self.num_scales):
            diff, occ_mask = diff_list[scale], occ_mask_list[scale]
            divider = occ_mask.mean((1,2,3))
            img_diff = diff * occ_mask.repeat(1,3,1,1)
            loss_pixel = img_diff.mean((1,2,3)) / (divider + 1e-12) # (B)
            loss_list.append(loss_pixel[:,None])
        loss = torch.cat(loss_list, 1).sum(1) # (B)
        return loss

    def cauchy_kernel(self, x, c=1):
        x_2 = torch.pow(x,2)
        res = c*c*torch.log(1+x_2/c/c)
        return res

    def compute_diff_weight(self, img_pyramid_from_l, img_pyramid, img_pyramid_from_r):
        diff_fwd = []
        diff_bwd = []
        weight_fwd = []
        weight_bwd = []
        valid_bwd = []
        valid_fwd = []
        for scale in range(self.num_scales):
            img_from_l, img, img_from_r = img_pyramid_from_l[scale], img_pyramid[scale], img_pyramid_from_r[scale]
            
            valid_pixels_fwd = 1 - (img_from_r == 0).prod(1, keepdim=True).type_as(img_from_r)
            valid_pixels_bwd = 1 - (img_from_l == 0).prod(1, keepdim=True).type_as(img_from_l)

            valid_bwd.append(valid_pixels_bwd)
            valid_fwd.append(valid_pixels_fwd)

            img_diff_l = torch.abs((img-img_from_l)).mean(1, True)
            img_diff_r = torch.abs((img-img_from_r)).mean(1, True)

            diff_cat = torch.cat((img_diff_l, img_diff_r),1)
            weight = 1 - nn.functional.softmax(diff_cat,1)
            weight = Variable(weight.data,requires_grad=False)

            # weight = (weight > 0.48).float()

            weight = 2*torch.exp(-(weight-0.5)**2/0.03)

            weight_bwd.append(torch.unsqueeze(weight[:,0,:,:],1) * valid_pixels_bwd)
            weight_fwd.append(torch.unsqueeze(weight[:,1,:,:],1) * valid_pixels_fwd)


            # diff_fwd.append(img_diff_r)
            # diff_bwd.append(img_diff_l)

            diff_fwd.append(self.cauchy_kernel(img_diff_r))
            diff_bwd.append(self.cauchy_kernel(img_diff_l))
        return diff_bwd, diff_fwd, weight_bwd, weight_fwd


    def compute_loss_ssim(self, img_pyramid, img_warped_pyramid, occ_mask_list):
        loss_list = []
        for scale in range(self.num_scales):
            img, img_warped, occ_mask = img_pyramid[scale], img_warped_pyramid[scale], occ_mask_list[scale]
            divider = occ_mask.mean((1,2,3))
            occ_mask_pad = occ_mask.repeat(1,3,1,1)
            ssim = SSIM(img * occ_mask_pad, img_warped * occ_mask_pad)
            loss_ssim = torch.clamp((1.0 - ssim) / 2.0, 0, 1).mean((1,2,3))
            loss_ssim = loss_ssim / (divider + 1e-12)
            loss_list.append(loss_ssim[:,None])
        loss = torch.cat(loss_list, 1).sum(1)
        return loss



    def gradients(self, img):
        dy = img[:,:,1:,:] - img[:,:,:-1,:]
        dx = img[:,:,:,1:] - img[:,:,:,:-1]
        return dx, dy

    def cal_grad2_error(self, flow, img):
        img_grad_x, img_grad_y = self.gradients(img)
        w_x = torch.exp(-10.0 * torch.abs(img_grad_x).mean(1).unsqueeze(1))
        w_y = torch.exp(-10.0 * torch.abs(img_grad_y).mean(1).unsqueeze(1))

        dx, dy = self.gradients(flow)
        dx2, _ = self.gradients(dx)
        _, dy2 = self.gradients(dy)
        error = (w_x[:,:,:,1:] * torch.abs(dx2)).mean((1,2,3)) + (w_y[:,:,1:,:] * torch.abs(dy2)).mean((1,2,3))
        #error = (w_x * torch.abs(dx)).mean((1,2,3)) + (w_y * torch.abs(dy)).mean((1,2,3))
        return error / 2.0

    def compute_loss_flow_smooth(self, optical_flows, img_pyramid):
        loss_list = []
        for scale in range(self.num_scales):
            flow, img = optical_flows[scale], img_pyramid[scale]
            #error = self.cal_grad2_error(flow, img)
            error = self.cal_grad2_error(flow/20.0, img)
            loss_list.append(error[:,None])
        loss = torch.cat(loss_list, 1).sum(1)
        return loss


    def compute_loss_flow_consis(self, fwd_flow_pyramid, bwd_flow_pyramid, occ_mask_list):
        loss_list = []
        for scale in range(self.num_scales):
            fwd_flow, bwd_flow, occ_mask = fwd_flow_pyramid[scale], bwd_flow_pyramid[scale], occ_mask_list[scale]
            fwd_flow_norm = self.get_flow_normalization(fwd_flow)
            bwd_flow_norm = self.get_flow_normalization(bwd_flow)
            bwd_flow_norm = Variable(bwd_flow_norm.data,requires_grad=False)
            occ_mask = 1-occ_mask

            divider = occ_mask.mean((1,2,3))
            
            loss_consis = (torch.abs(fwd_flow_norm+bwd_flow_norm) * occ_mask).mean((1,2,3))
            loss_consis = loss_consis / (divider + 1e-12)
            loss_list.append(loss_consis[:,None])
        loss = torch.cat(loss_list, 1).sum(1)
        return loss


    def inference_flow(self, img1, img2):
        img_hw = [img1.shape[2], img1.shape[3]]
        feature_list_1, feature_list_2 = self.fpyramid(img1), self.fpyramid(img2)
        optical_flow = self.pwc_model(feature_list_1, feature_list_2, img_hw)[0]
        return optical_flow
    
    def inference_corres(self, img1, img2):
        batch_size, img_h, img_w = img1.shape[0], img1.shape[2], img1.shape[3]
        
        # get the optical flows and reverse optical flows for each pair of adjacent images
        feature_list_1, feature_list_2 = self.fpyramid(img1), self.fpyramid(img2)
        optical_flows = self.pwc_model(feature_list_1, feature_list_2, [img_h, img_w])
        optical_flows_rev = self.pwc_model(feature_list_2, feature_list_1, [img_h, img_w])

        # get occlusion masks
        img2_visible_masks, img1_visible_masks = self.get_visible_masks(optical_flows, optical_flows_rev)
        # get consistent masks
        img2_consis_masks, img1_consis_masks, fwd_flow_diff_pyramid, bwd_flow_diff_pyramid = self.get_consistent_masks(optical_flows, optical_flows_rev)
        # get final valid masks 
        img2_valid_masks, img1_valid_masks = [], []
        for i, (img2_visible_mask, img1_visible_mask, img2_consis_mask, img1_consis_mask) in enumerate(zip(img2_visible_masks, img1_visible_masks, img2_consis_masks, img1_consis_masks)):
            img2_valid_masks.append(img2_visible_mask * img2_consis_mask)
            img1_valid_masks.append(img1_visible_mask * img1_consis_mask)
        
        return optical_flows[0], optical_flows_rev[0], img1_valid_masks[0], img2_valid_masks[0], fwd_flow_diff_pyramid[0], bwd_flow_diff_pyramid[0]

    def forward(self, inputs, output_flow=False, use_flow_loss=True, is_second_phase=False):
        images = inputs
        assert (images.shape[1] == 3)
        img_h, img_w = int(images.shape[2] / 3), images.shape[3] 
        imgl, img, imgr = images[:,:,:img_h,:], images[:,:,img_h:2*img_h,:], images[:,:,2*img_h:3*img_h,:]
        batch_size = imgl.shape[0]

        #pdb.set_trace()
        # get the optical flows and reverse optical flows for each pair of adjacent images
        feature_list_l, feature_list, feature_list_r = self.fpyramid(imgl), self.fpyramid(img), self.fpyramid(imgr)
        
        optical_flows_bwd = self.pwc_model(feature_list, feature_list_l, [img_h, img_w])
        #optical_flows_bwd_rev = self.pwc_model(feature_list_l, feature_list, [img_h, img_w])
        optical_flows_fwd = self.pwc_model(feature_list, feature_list_r, [img_h, img_w])
        #optical_flows_fwd_rev = self.pwc_model(feature_list_r, feature_list, [img_h, img_w])


        #cv2.imwrite('./meta/imgl.png', np.transpose(255*imgl[0].cpu().detach().numpy(), [1,2,0]).astype(np.uint8))
        #cv2.imwrite('./meta/img.png', np.transpose(255*img[0].cpu().detach().numpy(), [1,2,0]).astype(np.uint8))
        #cv2.imwrite('./meta/imgr.png', np.transpose(255*imgr[0].cpu().detach().numpy(), [1,2,0]).astype(np.uint8))

        
        loss_pack = {}
        # warp images
        imgl_pyramid = self.generate_img_pyramid(imgl, len(optical_flows_fwd))
        img_pyramid = self.generate_img_pyramid(img, len(optical_flows_fwd))
        imgr_pyramid = self.generate_img_pyramid(imgr, len(optical_flows_fwd))

        img_warped_pyramid_from_l = self.warp_flow_pyramid(imgl_pyramid, optical_flows_bwd)
        #imgl_warped_pyramid_from_ = self.warp_flow_pyramid(img_pyramid, optical_flows_bwd_rev)
        img_warped_pyramid_from_r = self.warp_flow_pyramid(imgr_pyramid, optical_flows_fwd)
        #imgr_warped_pyramid_from_ = self.warp_flow_pyramid(img_pyramid, optical_flows_fwd_rev)



        diff_bwd, diff_fwd, weight_bwd, weight_fwd = self.compute_diff_weight(img_warped_pyramid_from_l, img_pyramid, img_warped_pyramid_from_r)
        loss_pack['loss_pixel'] = self.compute_loss_with_mask(diff_fwd, weight_fwd) + \
            self.compute_loss_with_mask(diff_bwd, weight_bwd)
        
        loss_pack['loss_ssim'] = self.compute_loss_ssim(img_pyramid, img_warped_pyramid_from_r, weight_fwd) + \
            self.compute_loss_ssim(img_pyramid, img_warped_pyramid_from_l,weight_bwd)
        #loss_pack['loss_ssim']  = torch.zeros([2]).to(imgl.get_device()).requires_grad_()

        loss_pack['loss_flow_smooth'] = self.compute_loss_flow_smooth(optical_flows_fwd, img_pyramid)  + \
            self.compute_loss_flow_smooth(optical_flows_bwd, img_pyramid)
        
        loss_pack['loss_flow_consis'] = self.compute_loss_flow_consis(optical_flows_fwd, optical_flows_bwd, weight_fwd)
        # loss_pack['loss_flow_consis'] = torch.zeros([2]).to(imgl.get_device()).requires_grad_()


        if output_flow:
            return loss_pack#, optical_flows_fwd[0], optical_flows_bwd[0], img1_valid_masks[0], img2_valid_masks[0], fwd_flow_diff_pyramid[0], bwd_flow_diff_pyramid[0]
        else:
            return loss_pack
