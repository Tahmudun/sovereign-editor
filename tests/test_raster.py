"""Pixel-level checks for depth, cutouts, alpha seams and texture wrap modes."""
import numpy as np

from sovereign_editor.nitro import Primitive
from sovereign_editor.raster import render, _sample_axis


def square(rgb, depth, alpha=1, texture=None):
    return Primitive(np.array([[0, 0, depth], [4, 0, depth], [4, 4, depth], [0, 4, depth]], dtype=float),
                     np.tile(rgb, (4, 1)), np.array([[0, 0], [1, 0], [1, 1], [0, 1]]),
                     np.array([[0, 1, 2], [0, 2, 3]]),
                     {"alpha": alpha, "repeat": [False, False], "mirror": [False, False]}, texture)


def test_occlusion_and_transparent_cutout():
    red, blue = square([1, 0, 0], 1), square([0, 0, 1], 0)
    image = np.asarray(render([red, blue], (4, 4), np.eye(3)))
    assert np.all(image == [255, 0, 0])
    cutout = np.array([[[255, 255, 255, 0], [255, 255, 255, 255]]], dtype=np.uint8)
    red.texture = cutout
    image = np.asarray(render([red, blue], (4, 4), np.eye(3)))
    assert np.all(image[:, :2] == [0, 0, 255]) and np.all(image[:, 2:] == [255, 0, 0])


def test_alpha_shared_diagonal_is_blended_once_and_depth_sorted():
    red, blue = square([1, 0, 0], 1, .5), square([0, 0, 1], 0)
    a = np.asarray(render([red, blue], (4, 4), np.eye(3)))
    b = np.asarray(render([blue, red], (4, 4), np.eye(3)))
    assert np.array_equal(a, b) and np.all(a == [188, 0, 188])


def test_repeat_mirror_and_clamp_include_negative_coordinates():
    uv = np.array([-.25, 0, .75, 1, 1.25, 2.25])
    assert _sample_axis(uv, 4, True, False).tolist() == [3, 0, 3, 0, 1, 1]
    assert _sample_axis(uv, 4, True, True).tolist() == [0, 0, 3, 3, 2, 1]
    assert _sample_axis(uv, 4, False, False).tolist() == [0, 0, 3, 3, 3, 3]
