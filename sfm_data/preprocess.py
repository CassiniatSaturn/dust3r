# resize image  to 512*384 and save to sfm_data/resize_image
import os
import cv2
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.image as mpimg
from PIL import Image
import glob


def resize_image():
    image_path = "sfm_data/box_images"
    resize_image_path = "sfm_data/box_images"
    if not os.path.exists(resize_image_path):
        os.makedirs(resize_image_path)
    for filename in os.listdir(image_path):
        img = cv2.imread(os.path.join(image_path, filename))
        img = cv2.resize(img, (512, 288))
        cv2.imwrite(os.path.join(resize_image_path, filename), img)
        print("resize image:", filename)


# resize_image()
