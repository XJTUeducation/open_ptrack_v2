#!/usr/bin/python
import cv2
import sys
import dlib
import numpy
import cProfile
import multiprocessing

import tf
import rospy
import rospkg
import cv_bridge
import message_filters
from std_msgs.msg import *
from sensor_msgs.msg import *
from geometry_msgs.msg import *
from opt_msgs.msg import *

from dynamic_reconfigure.server import Server
from recognition.cfg import FaceDetectionConfig

import recognition_utils as recutils


# this node performs 2D face detection on the ROIs calculated from the people detection result
# it replaces the DetectionArray/detections/box_2D with the detected face regions while do not change the other members of the DetectionArray
# then, it outputs the modified DetectionArray as an array of face detection results
# note that the replaced box_2d is on the color image coordinate
class FaceDetectionNode:
	def __init__(self, sensor_name):
		self.sensor_name = sensor_name

		self.cfg_server = Server(FaceDetectionConfig, self.cfg_callback)
		self.cv_bridge = cv_bridge.CvBridge()
		self.detector = dlib.fhog_object_detector(self.face_detector_path)
		self.pool = multiprocessing.Pool(3)

		# get transformation between world, color, and depth images
		now = rospy.Time(0)
		tf_listener = tf.TransformListener()
		print self.sensor_name
		self.ir2rgb = recutils.lookupTransform(tf_listener, self.sensor_name + '_ir_optical_frame', self.sensor_name + '_rgb_optical_frame', 10.0, now)
		# self.ir2rgb = numpy.eye(4, 4).astype(numpy.float64)
		print '--- ir2rgb ---\n', self.ir2rgb
		self.world2rgb = recutils.lookupTransform(tf_listener, '/world', self.sensor_name + '_rgb_optical_frame', 10.0, now)
		print '--- world2rgb ---\n', self.world2rgb

		self.pub = rospy.Publisher('/face_detector/detections', DetectionArray, queue_size=10)
		self.pub_local = rospy.Publisher(self.sensor_name + '/face_detector/detections', DetectionArray, queue_size=10)

		try:
			print 'tryingnsecs_round to listen raw rgb image topic...'
			rospy.client.wait_for_message(self.sensor_name + '/rgb/image', Image, 1.0)
			img_subscriber = message_filters.Subscriber(self.sensor_name + '/rgb/image', Image)
		except rospy.ROSException:
			print 'failed, listen compressed rgb image topic'
			img_subscriber = message_filters.Subscriber(self.sensor_name + '/rgb/image/compressed', CompressedImage)

		self.subscribers = [
			img_subscriber,
			message_filters.Subscriber(self.sensor_name + '/rgb/camera_info', CameraInfo),
			message_filters.Subscriber('/detector/detections', DetectionArray)
		]

		# TypeSynchronizer doesn't work, the image time and the detection time are slightly different?
		# self.ts = message_filters.TimeSynchronizer(self.subscribers, 5)
		# self.ts = message_filters.ApproximateTimeSynchronizer(self.subscribers, 5, 0.0001)
		self.ts = recutils.TimeSynchronizer(self.subscribers, 60, 1000)
		self.ts.registerCallback(self.callback)

		self.reset_time_sub = rospy.Subscriber('/reset_time', Empty, self.reset_time)

	# callback for dynamic configure
	def cfg_callback(self, config, level):
		package_path = rospkg.RosPack().get_path('recognition')
		self.face_detector_path = package_path + config.face_detector_path		# the path to the face detector model file
		self.confidence_thresh = config.confidence_thresh				# the threshold for confidence of face detection
		self.roi_width = config.roi_width_								# the width of a face detection ROI in the world space [m]
		self.calc_roi_from_top = config.calc_roi_from_top				# if true, ROIs are calculated from the top positions of detected clusters
		self.head_offset_z_top = config.head_offset_z_top				# the distance between the top position of a human cluster and the center of the face [m]
		self.head_offset_z_centroid = config.head_offset_z_centroid		# the distance between the centroid of a human cluster and the center of the face [m]
		self.upscale_minsize = config.upscale_minsize					# the face detection ROI is upscaled so that its width get larger than #upscale_minsize
		self.visualization = config.visualization						# if true, the visualization of the detection will be shown

		print '--- cfg_callback ---'
		print 'confidence_thresh', config.confidence_thresh
		print 'roi_width', config.roi_width_
		print 'calc_roi_from_top', config.calc_roi_from_top
		print 'head_offset_z_top', config.head_offset_z_top
		print 'head_offset_z_centroid', config.head_offset_z_centroid
		print 'upscale_minsize', config.upscale_minsize
		print 'visualization', config.visualization
		return config

	def reset_time(self, msg):
		print 'reset time'
		self.ts = message_filters.ApproximateTimeSynchronizer(self.subscribers, 200, 0.00001)
		self.ts.registerCallback(self.callback)

	# callback
	def callback(self, rgb_image_msg, rgb_info_msg, detection_msg):
		if detection_msg.header.frame_id != self.sensor_name + '_ir_optical_frame':
			print 'frame_ids not matched'
			return

		t1 = rospy.Time.now()

		# read rgb image
		if type(rgb_image_msg) is CompressedImage:
			rgb_image = recutils.decompress(rgb_image_msg)
		else:
			rgb_image = self.cv_bridge.imgmsg_to_cv2(rgb_image_msg)
		gray_image = cv2.cvtColor(rgb_image, cv2.COLOR_BGR2GRAY)

		# calculate ROIs and then run the 2D face detector
		rois = self.calc_rois(rgb_info_msg, detection_msg)
		faces = map(lambda x: self.detect_face(gray_image, x), rois)
		#print(len(faces))
		#print(faces)

		# publish the face detection result
		for face, detection in zip(faces, detection_msg.detections):
			if face is None:
				detection.box_2D = BoundingBox2D(x=0, y=0, width=0, height=0)
			else:
				detection.box_2D = BoundingBox2D(x=face[0], y=face[1], width=face[2]-face[0], height=face[3]-face[1])
		self.pub.publish(detection_msg)
		self.pub_local.publish(detection_msg)

		t2 = rospy.Time.now()

		print "test"

		if self.visualization:
			self.visualize(rgb_image, rois, faces, (t2 - t1).to_sec())

	# visualizes the detection result
	def visualize(self, rgb_image, rois, faces, processing_time):
		for roi in rois:
			cv2.rectangle(rgb_image, (roi[0], roi[1]), (roi[2], roi[3]), (0, 255, 0), 4)

		for face in faces:
			if face is None:
				continue
			cv2.rectangle(rgb_image, (face[0], face[1]), (face[2], face[3]), (0, 0, 255), 4)

		factor = rgb_image.shape[1] / 480
		rgb_image = cv2.resize(rgb_image, (rgb_image.shape[1]/factor, rgb_image.shape[0]/factor))

		text = '%.2fmsec / %.2ffps' % (processing_time * 1000.0, (1.0 / max(processing_time, 0.0001)))
		cv2.putText(rgb_image, text, (10, 15), cv2.FONT_HERSHEY_PLAIN, 0.8, (64, 64, 64), 3)
		cv2.putText(rgb_image, text, (10, 15), cv2.FONT_HERSHEY_PLAIN, 0.8, (255, 255, 255))

		cv2.imshow('rgb_image', rgb_image)
		cv2.waitKey(2)

	# calculate ROIs from the top positions of human clusters
	def calc_rois(self, rgb_info_msg, detection_msg):
		# the vector between the top position of a cluster and the center of the face
		head_offset_z = self.head_offset_z_top if self.calc_roi_from_top else self.head_offset_z_centroid
		head_offset = numpy.dot(self.world2rgb[:3, :3], [0, 0, head_offset_z])
		head_offset = numpy.array([head_offset[0], head_offset[1], head_offset[2], 0.0])

		# calculate the face positions from the detected clusters
		face_positions = []
		for detection in detection_msg.detections:
			if self.calc_roi_from_top:
				top_pt = [detection.top.x, detection.top.y, detection.top.z, 1.0]
			else:
				top_pt = [detection.centroid.x, detection.centroid.y, detection.centroid.z, 1.0]
			top_pt += head_offset
			face_positions.append(top_pt)

		if len(face_positions) == 0:
			return []

		# transform the positions from the IR coordinate to the RGB coordinate
		face_positions = numpy.transpose(numpy.dot(self.ir2rgb, numpy.transpose(face_positions))[:3, :])

		# project the face positions on the image
		rvec = numpy.array([0, 0, 0], dtype=numpy.float64)
		tvec = numpy.array([0, 0, 0], dtype=numpy.float64)
		camera_matrix = numpy.array(rgb_info_msg.K, dtype=numpy.float64).reshape(3, 3)
		distortion = numpy.array(rgb_info_msg.D, dtype=numpy.float64)

		projected = cv2.projectPoints(face_positions.astype(numpy.float64), rvec, tvec, camera_matrix, distortion)[0]

		# calculate the ROIs
		rois = []
		for i in range(len(detection_msg.detections)):
			roi = self.calc_roi(rgb_info_msg, self.roi_width, face_positions[i], projected[i, 0, :])
			rois.append(roi)
		return rois

	# calculate ROI from a 3D position
	def calc_roi(self, rgb_info_msg, w, xyz, uv):
		# project the roi_width from the world coordinate[m] to the image coordinate[pix]
		half_w = w * rgb_info_msg.K[0] / xyz[2]
		left = int(uv[0] - half_w)
		top = int(uv[1] - half_w)
		right = int(uv[0] + half_w)
		bottom = int(uv[1] + half_w)

		# ROI range check
		left = min(rgb_info_msg.width, max(0, left))
		top = min(rgb_info_msg.height, max(0, top))
		right = min(rgb_info_msg.width, max(0, right))
		bottom = min(rgb_info_msg.height, max(0, bottom))

		width = max(0, right - left)
		height = max(0, bottom - top)

		return (left, top, left + width, top + height)

	# detect a face on an ROI
	# return None if no face is detected
	def detect_face(self, gray_image, roi_rect):
		# check if the ROI is valid
		if roi_rect[2] <= roi_rect[0] or roi_rect[3] <= roi_rect[1]:
			return None

		# detection
		print "test2"
		roi = gray_image[roi_rect[1]:roi_rect[3], roi_rect[0]:roi_rect[2]]
		# roi = roi.reshape(roi.shape[0], roi.shape[1]).astype(numpy.uint8)

		scaling_factor = 1.0
		width = roi_rect[2] - roi_rect[0]
		if width < self.upscale_minsize:
			scaling_factor = float(width) / self.upscale_minsize
			roi = cv2.resize(roi, (self.upscale_minsize, self.upscale_minsize))
		detected, scores, idx = self.detector.run(roi, 0, self.confidence_thresh)
		if len(detected) <= 0:
			return None

		return (
			int(scaling_factor * detected[0].left()) + roi_rect[0],
			int(scaling_factor * detected[0].top()) + roi_rect[1],
			int(scaling_factor * detected[0].right()) + roi_rect[0],
			int(scaling_factor * detected[0].bottom()) + roi_rect[1]
		)


def main():
	sensor_name = '/kinect2_head' if len(sys.argv) < 2 else '/' + sys.argv[1]
	print 'sensor_name', sensor_name

	rospy.init_node('face_detection_node_' + sensor_name[1:])
	node = FaceDetectionNode(sensor_name)
	rospy.spin()

if __name__ == '__main__':
	main()
