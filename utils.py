import re
import numpy as np
import struct
from tqdm import tqdm
from PIL import Image
import os
import cv2
import itertools
from ctypes import (
    c_float,
    c_uint8,
    cdll,
    c_int32,
    c_void_p,
    Structure,
    POINTER,
    cast,
    byref,
    pointer
)


def get_tag(tag,bytes, tag_bytes=None, tag_data_bytes=None):
    if tag_bytes is None and tag_data_bytes is None:
        tag_bytes = bytearray()
        tag_data_bytes = bytearray()
    tag_pos = re.search(tag,bytes).span()[0]
    len = bytes[tag_pos+2:tag_pos+4]
    t_start_pos = tag_pos
    td_start_pos = tag_pos+4
    stop_pos = tag_pos+int.from_bytes(len,byteorder='big')+2
    tag_bytes = tag_bytes+bytes[t_start_pos:stop_pos]
    tag_data_bytes = tag_data_bytes+bytes[td_start_pos:stop_pos]
    if bytes[stop_pos:stop_pos+2]==tag:
        tag_bytes, tag_data_bytes = get_tag(tag,bytes[stop_pos:], tag_bytes, tag_data_bytes)
    return tag_bytes, tag_data_bytes

def bytes2float32(bytes):
    format = '{:d}f'.format(len(bytes)//4)
    return np.array(struct.unpack(format, bytes))

def bytes2uint16(bytes):
    format = '{:d}H'.format(len(bytes)//2)
    return np.array(struct.unpack(format, bytes))

def bytes2int16(bytes):
    format = '{:d}h'.format(len(bytes)//2)
    return np.array(struct.unpack(format, bytes))

def ndvi2emissivity(ndvi_arr):
    shape = ndvi_arr.shape
    ndvi_arr = ndvi_arr.flatten()
    _ndvi_soil = 0.157
    _ndvi_veg = 0.905
    _e_soil = 0.935
    _e_veg = 0.988
    e_arr = np.empty(len(ndvi_arr))
    for i, ndvi in enumerate(ndvi_arr):
        if ndvi<_ndvi_soil:
            e_arr[i]=_e_soil
        elif ndvi>_ndvi_veg:
            e_arr[i]=_e_veg
        else:
            p_v = ((ndvi-_ndvi_soil)/(_ndvi_veg-_ndvi_soil))**2
            e_arr[i]=_e_veg*p_v+_e_soil*(1-p_v)
    return np.reshape(e_arr,shape)

class TSDK:
    class c_measurement_params_struct(Structure):
        _fields_ = [("distance", c_float),
                    ("humidyty", c_float),
                    ("emissivity", c_float),
                    ("reflection", c_float)]

    def __init__(self, verbose):
        self.lib = cdll.LoadLibrary("./libdirp.dll")
        self.lib.dirp_register_app(b"DJI_TSDK")
        c_verbose = c_int32(verbose)
        self.lib.dirp_set_verbose_level(c_verbose)
        self.shape = (512,640)
        self.temp_data_len = self.shape[0]*self.shape[1]
        self.c_temp_data_size = c_int32(4*self.temp_data_len)
        c_temp_data = (c_float*self.temp_data_len)()
        self.c_temp_data_pointer = cast(c_temp_data, POINTER(c_float))
        self.c_dirp_handle = c_void_p()
        self.c_measurement_params = self.c_measurement_params_struct()
    def load_rjpg(self, path):
        self.file_path = path
        self.file_name = os.path.basename(path)
        with open(path,"rb") as file:
            rjpg_bytes = bytearray(file.read())
            rjpg_size = len(rjpg_bytes)
        c_rjpeg_bytes = (c_uint8*rjpg_size)(*rjpg_bytes)
        c_rjpeg_size = c_int32(rjpg_size)
        self.lib.dirp_create_from_rjpeg(byref(c_rjpeg_bytes), c_rjpeg_size, byref(self.c_dirp_handle))
    def sweep_setup(self, d_list=None, h_list=None, e_list=None, r_list=None):
        self.d_list = d_list
        self.h_list = h_list
        self.e_list = e_list
        self.r_list = r_list
        a = [d_list, h_list, e_list, r_list]
        self.configurations = list(itertools.product(*a))
        return self.configurations
    def sweep_run(self):
        self.sweep_temp_data = []
        for configuration in self.configurations:
            self.sweep_temp_data.append(self.measure(configuration[0],configuration[1],configuration[2],configuration[3]))
        return self.sweep_temp_data
    def mask_temp_from_sweep_data(self, mask_path):
        assert os.path.isfile(mask_path)
        im_gray = cv2.imread(mask_path, cv2.IMREAD_GRAYSCALE)
        
        (thresh, im_bw) = cv2.threshold(im_gray, 128, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
        self.sweep_mask_mean = []
        for temp_data in self.sweep_temp_data:
            self.sweep_mask_mean.append(cv2.mean(temp_data, im_bw)[0])
        return self.sweep_mask_mean
    def sweep_save_tifs(self, dir):
        for di, x0 in enumerate(self.sweep_temp_data):
            for hi, x1 in enumerate(x0):
                for ei, x2 in enumerate(x1):
                    for ri, arr in enumerate(x2):
                        im = Image.fromarray(arr)
                        tif_path = f"{dir}/{self.file_name.split('.')[0]}_d{self.d_list[di]}_h{self.h_list[hi]}_e{self.e_list[ei]}_r{self.r_list[ri]}.tiff"
                        im.save(tif_path)
                        # copy exif data from original file to new tiff
                        os.system(f"exiftool.exe -tagsfromfile {self.file_path} {tif_path} -overwrite_original_in_place")
    def measure(self, d=10.0, h=77.0, e=0.95, r=21.0):
        self.c_measurement_params.distance = d
        self.c_measurement_params.humidyty = h
        self.c_measurement_params.emissivity = e
        self.c_measurement_params.reflection = r
        self.lib.dirp_set_measurement_params(self.c_dirp_handle, byref(self.c_measurement_params))
        self.lib.dirp_measure_ex(self.c_dirp_handle, self.c_temp_data_pointer, self.c_temp_data_size)
        return np.reshape(np.array(self.c_temp_data_pointer[:self.temp_data_len]),self.shape)
    