"""Oversized inference inputs keep source dimensions and normalized boxes."""
import importlib.util
from pathlib import Path
import sys
import unittest
import numpy as np

root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root))
spec=importlib.util.spec_from_file_location('large_image_analysis',root/'backEnd/UserMediaAnalysis.py')
analysis=importlib.util.module_from_spec(spec)
spec.loader.exec_module(analysis)

class Client:
    def __init__(self): self.shapes=[]
    def yolo(self, image, conf):
        self.shapes.append(image.shape)
        h,w=image.shape[:2]
        return [('car',.9,0,0,w,h)]

class LargeImageChecks(unittest.TestCase):
    def test_oversized_frame_resizes_only_inference_and_preserves_box_coordinates(self):
        image=np.zeros((4000,6000,3),dtype=np.uint8)
        client=Client()
        result=analysis.analyzeFrame(image,client,{},0)
        h,w,_=client.shapes[0]
        self.assertLessEqual(h*w,16000000)
        self.assertEqual(image.shape,(4000,6000,3))
        self.assertAlmostEqual(w/h,1.5,places=3)
        self.assertEqual([result[0][key] for key in ('x1','y1','x2','y2')],[0,0,1,1])
    def test_small_frame_keeps_its_resolution(self):
        client=Client()
        analysis.analyzeFrame(np.zeros((600,800,3),dtype=np.uint8),client,{})
        self.assertEqual(client.shapes,[(600,800,3)])

if __name__=='__main__': unittest.main(verbosity=2)
