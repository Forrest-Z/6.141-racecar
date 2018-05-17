import rospy
import math
import numpy as np
from yaml import load
from visualization_msgs.msg import Marker, MarkerArray
from geometry_msgs.msg import Vector3, Point, Pose, PoseStamped, PoseArray, Quaternion, PolygonStamped, Polygon, Point32, PoseWithCovarianceStamped, PointStamped
from std_msgs.msg import Header, ColorRGBA
from nav_msgs.msg import OccupancyGrid

import json, time, collections, recordclass

import tf.transformations
import tf

import skimage.morphology
from scipy import ndimage

EPSILON = 0.00000000001

Circle = collections.namedtuple("Circle", ["radius", "center", "angle", "deflection"])
Path  = collections.namedtuple("Path", ["states"])
SearchNode = collections.namedtuple("SearchNode", ["state", "cost", "heuristic", "parent"])
SearchNodeTree = collections.namedtuple("SearchNodeTree", ["state", "cost", "heuristic", "parent", "tree_node"])
TreeNode = recordclass.recordclass("TreeNode", ["state", "children"])

def norm(x, y):
  return math.sqrt(x**2 + y**2)

def distance(pt1, pt2):
  return math.sqrt((pt2[0]-pt1[0])**2 + (pt2[1]-pt1[1])**2)

def load_params_from_yaml(fp):
  with open(fp, 'r') as infile:
    yaml_data = load(infile)
    for param in yaml_data:
      print "param:", param, ":", yaml_data[param]
      rospy.set_param("~"+param, yaml_data[param])

def moving_average(a, n=10) :
  ret = np.cumsum(a, axis=0, dtype=float)
  ret[n:] = ret[n:] - ret[:-n]
  return ret[n - 1:] / n

def getParamOrFail(name):
  """
  Throws an error if a parameter cannot be found (avoid silent failure).
  """
  if rospy.search_param(name) is None:
    raise ParamNotFoundException()
  return rospy.get_param(name)

class ParamNotFoundException(Exception):
  pass

def make_header(frame_id, stamp=None):
  if stamp == None:
      stamp = rospy.Time.now()
  header = Header()
  header.stamp = stamp
  header.frame_id = frame_id
  return header


class CircularArray(object):
  def __init__(self, size):
    self.arr = np.zeros(size)
    self.ind = 0
    self.num_els = 0

  def append(self, value):
    if self.num_els < self.arr.shape[0]:
      self.num_els += 1
    self.arr[self.ind] = value
    self.ind = (self.ind + 1) % self.arr.shape[0]

  def mean(self):
    return np.mean(self.arr[:self.num_els])

  def median(self):
    return np.median(self.arr[:self.num_els])

class Timer:
  def __init__(self, smoothing):
    self.arr = CircularArray(smoothing)
    self.last_time = time.time()

  def tick(self):
    t = time.time()
    self.arr.append(1.0 / (t - self.last_time))
    self.last_time = t

  def fps(self):
    return self.arr.mean()

def quaternion_to_angle(q):
    """Convert a quaternion _message_ into an angle in radians.
    The angle represents the yaw.
    This is not just the z component of the quaternion."""
    x, y, z, w = q.x, q.y, q.z, q.w
    roll, pitch, yaw = tf.transformations.euler_from_quaternion((x, y, z, w))
    return yaw


class LineTrajectory(object):
  """ A class to wrap and work with piecewise linear trajectories. """
  def __init__(self, viz_namespace=None):
    self.points = []
    self.np_points = None
    self.distances = []
    self.speed_profile = []
    self.has_acceleration = False
    self.visualize = False
    self.viz_namespace = viz_namespace
    self.speed_interpolater = None

    # Visualization publishers.
    if viz_namespace:
      self.visualize = True
      self.start_pub = rospy.Publisher(viz_namespace + "/start_point", Marker, queue_size = 1)
      self.traj_pub  = rospy.Publisher(viz_namespace + "/path", Marker, queue_size = 1)
      self.end_pub   = rospy.Publisher(viz_namespace + "/end_pose", Marker, queue_size = 1)
      self.speed_pub = rospy.Publisher(viz_namespace + "/speeds", MarkerArray, queue_size = 1)

  def publish_waypoints(self):
    poly = self.toPolygon()
    self.waypoint_publisher.publish(poly)

  # compute the distances along the path for all path segments beyond those already computed
  def update_distances(self):
    num_distances = len(self.distances)
    num_points = len(self.points)

    for i in xrange(num_distances,num_points):
      if i == 0:
        self.distances.append(0)
      else:
        p0 = self.points[i-1]
        p1 = self.points[i]
        delta = np.array([p0[0]-p1[0],p0[1]-p1[1]])
        self.distances.append(self.distances[i-1] + np.linalg.norm(delta))

  def distance_to_end(self, t):
    if not len(self.points) == len(self.distances):
      print "WARNING: Different number of distances and points, this should never happen! Expect incorrect results. See LineTrajectory class."
    dat = self.distance_along_trajectory(t)
    if dat == None:
      return None
    else:
      return self.distances[-1] - dat

  def distance_along_trajectory(self, t):
    # compute distance along path
    # ensure path boundaries are respected
    if t < 0 or t > len(self.points) - 1.0:
      return None
    i = int(t) # which segment
    t = t % 1.0 # how far along segment
    if t < EPSILON:
      return self.distances[i]
    else:
      return (1.0-t)*self.distances[i] + t*self.distances[i+1]

  def addPoint(self, point):
    # rospy.loginfo("Adding point to trajectory: %s %s" % (point.x, point.y))
    self.points.append((point.x, point.y))
    self.update_distances()
    self.mark_dirty()

  def clear(self):
    self.points = []
    self.distances = []
    self.speed_profile = []
    self.speed_interpolater = None
    self.mark_dirty()

  def empty(self):
    return len(self.points) == 0

  def save(self, path):
    print "Saving trajectory to:", path
    data = {}
    data["points"] = []
    for p in self.points:
      data["points"].append({"x": p[0], "y": p[1]})
    with open(path, 'w') as outfile:
      json.dump(data, outfile)

  def mark_dirty(self):
    self.has_acceleration = False

  def dirty(self):
    return not self.has_acceleration

  def load(self, path):
    print "Loading trajectory:", path
    with open(path) as json_file:
      json_data = json.load(json_file)
      for p in json_data["points"]:
        self.points.append((p["x"], p["y"]))
    self.update_distances()
    print "Loaded:", len(self.points), "points"
    self.mark_dirty()

  # put the points into a KD tree for faster nearest neighbors queries
  def make_np_array(self):
    self.np_points = np.array(self.points)
    self.np_distances = np.array(self.distances)
    self.has_acceleration = True

  # build a trajectory class instance from a trajectory message
  def fromPolygon(self, trajMsg):
    for p in trajMsg.points:
      self.points.append((p.x, p.y))
      if p.z >= 0:
        self.speed_profile.append(p.z)
    self.update_distances()
    self.mark_dirty()
    print "Loaded new trajectory with:", len(self.points), "points"

  def toPolygon(self):
    poly = PolygonStamped()
    poly.header = make_header("/map")
    use_speed_profile = len(self.speed_profile) == len(self.points)
    for i in xrange(len(self.points)):
      p = self.points[i]
      pt = Point32()
      pt.x = p[0]
      pt.y = p[1]
      if use_speed_profile:
        pt.z = self.speed_profile[i]
      else:
        pt.z = -1
      poly.polygon.points.append(pt)
    return poly

  def publish_start_point(self, duration=0.0, scale=0.1):
    should_publish = len(self.points) > 0
    if self.visualize: # and self.speed_pub.get_num_connections() > 0:
      rospy.loginfo('Publish start point.')
      marker = Marker()
      marker.header = make_header("/map")
      marker.ns = self.viz_namespace + "/trajectory"
      marker.id = 0
      marker.type = 2 # sphere
      marker.lifetime = rospy.Duration.from_sec(duration)
      if should_publish: # Create marker.
        marker.action = 0
        marker.pose.position.x = self.points[0][0]
        marker.pose.position.y = self.points[0][1]
        marker.pose.orientation.w = 1.0
        marker.scale.x = 1.0
        marker.scale.y = 1.0
        marker.scale.z = 1.0
        marker.color.r = 0.0
        marker.color.g = 1.0
        marker.color.b = 0.0
        marker.color.a = 1.0
      else:
        marker.action = 2 # Delete marker.
      self.start_pub.publish(marker)

  def publish_end_point(self, duration=0.0):
    should_publish = len(self.points) > 1
    if self.visualize: # and self.end_pub.get_num_connections() > 0:
      rospy.loginfo('Publishing end point.')
      marker = Marker()
      marker.header = make_header("/map")
      marker.ns = self.viz_namespace + "/trajectory"
      marker.id = 1
      marker.type = 2 # sphere
      marker.lifetime = rospy.Duration.from_sec(duration)
      if should_publish:
        marker.action = 0
        marker.pose.position.x = self.points[-1][0]
        marker.pose.position.y = self.points[-1][1]
        marker.pose.orientation.w = 1.0
        marker.scale.x = 1.0
        marker.scale.y = 1.0
        marker.scale.z = 1.0
        marker.color.r = 1.0
        marker.color.g = 0.0
        marker.color.b = 0.0
        marker.color.a = 1.0
      else:
        marker.action = 2
      self.end_pub.publish(marker)

  def publish_trajectory(self, duration=0.0):
    should_publish = len(self.points) > 1
    if self.visualize: # and self.traj_pub.get_num_connections() > 0:
      print "Publishing trajectory"
      marker = Marker()
      marker.header = make_header("/map")
      marker.ns = self.viz_namespace + "/trajectory"
      marker.id = 2
      marker.type = 4 # line strip
      marker.lifetime = rospy.Duration.from_sec(duration)
      if should_publish:
        marker.action = 0
        marker.scale.x = 0.3
        marker.scale.y = 0.3
        marker.scale.z = 0.05
        marker.color.r = 0.0
        marker.color.g = 1.0
        marker.color.b = 0.0
        marker.color.a = 0.75
        for p in self.points:
          pt = Point32()
          pt.x = p[0]
          pt.y = p[1]
          pt.z = -0.1
          marker.points.append(pt)
      else:
        marker.action = 2
      self.traj_pub.publish(marker)
    elif self.traj_pub.get_num_connections() == 0:
      print "Not publishing trajectory, no subscribers"

  def publish_speeds(self, duration=0.0, scale=0.7):
    should_publish = len(self.speed_profile) > 1
    if self.visualize and self.speed_pub.get_num_connections() > 0:
      if self.dirty():
        self.make_np_array()
      markers = [marker_clear_all("/map")]
      normed_speeds = np.array(self.speed_profile) / np.max(self.speed_profile)
      last_speed = 0.0
      for i, speed in enumerate(normed_speeds):
        if speed >= last_speed * 0.99:
          color = ColorRGBA(0, 1, 0, 0.8)
        else:
          color = ColorRGBA(1, 0, 0, 0.8)
        last_speed = speed
        markers.append(marker_from_point_radius(self.np_points[i,:], np.power(speed, 0.8) * scale,
          index=i, linewidth=0.05, color=color, lifetime=duration))

      marker_array = MarkerArray(markers=markers)
      self.speed_pub.publish(marker_array)

  def speed_at_t(self, t):
    if self.speed_interpolater == None:
      self.xs = np.arange(len(self.speed_profile))
      self.ys = self.speed_profile
      self.speed_interpolater = lambda x: np.interp(x,  self.xs, self.ys)

    return self.speed_interpolater(t)

  def publish_viz(self, duration=0):
    """
    Publishes to all of the visualization topics.
    """
    if not self.visualize:
      rospy.loginfo("Cannot visualize path, not initialized with visualization enabled.")
      return

    self.publish_start_point(duration=duration)
    self.publish_trajectory(duration=duration)
    self.publish_end_point(duration=duration)
    self.publish_speeds(duration=duration)

def angle_to_quaternion(angle):
    return Quaternion(*tf.transformations.quaternion_from_euler(0, 0, angle))

