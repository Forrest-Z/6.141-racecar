#!/usr/bin/env python

import rospy
import numpy as np
import time
import utils
import math

from geometry_msgs.msg import PolygonStamped, PoseStamped, Point32
from visualization_msgs.msg import Marker
from ackermann_msgs.msg import AckermannDriveStamped, AckermannDrive
from nav_msgs.msg import Odometry
from std_msgs.msg import String

class PurePursuit(object):
  """
  Implements Pure Pursuit trajectory tracking with a fixed lookahead and speed.
  """
  PERCENT_SLIP_SPEED = utils.getParamOrFail("/obstacle_avoidance/pure_pursuit/percent_slip_speed")
  PERCENT_SLIP_SPEED_INSTANTANEOUS = utils.getParamOrFail("/obstacle_avoidance/pure_pursuit/percent_slip_speed_instantaneous")
  ZERO_THROTTLE_DECELERATION = utils.getParamOrFail("/obstacle_avoidance/pure_pursuit/zero_throttle_deceleration")

  TRAJECTORY_TOPIC = utils.getParamOrFail("/obstacle_avoidance/trajectory_topic")
  DRIVE_TOPIC = utils.getParamOrFail("/obstacle_avoidance/drive_topic")
  LOCALIZATION_TOPIC = utils.getParamOrFail("/obstacle_avoidance/localization_topic")

  MIN_LOOKAHEAD = utils.getParamOrFail("/obstacle_avoidance/pure_pursuit/min_lookahead")
  MAX_LOOKAHEAD = utils.getParamOrFail("/obstacle_avoidance/pure_pursuit/max_lookahead")
  WHEELBASE_LENGTH = float(utils.getParamOrFail("/obstacle_avoidance/pure_pursuit/wheelbase"))

  ANGLE_PROP_GAIN = utils.getParamOrFail("/obstacle_avoidance/pure_pursuit/angle_prop_gain")
  ANGLE_DERIV_GAIN = utils.getParamOrFail("/obstacle_avoidance/pure_pursuit/angle_deriv_gain")
  SPEED_GAIN = utils.getParamOrFail("/obstacle_avoidance/pure_pursuit/speed_gain")

  MAX_SPEED = utils.getParamOrFail("/obstacle_avoidance/pure_pursuit/max_speed")
  MAX_TURN = math.pi / 12.0

  TRAJECTORY_SLICE_SIZE = 0.1 # LOOKAHEAD / 10.0

  ACCEPTABLE_GOAL_ERROR = TRAJECTORY_SLICE_SIZE

  def __init__(self):
    rospy.loginfo('Trajectory topic: %s' % self.TRAJECTORY_TOPIC)

    # Publishers and subscribers.
    self.traj_sub = rospy.Subscriber(self.TRAJECTORY_TOPIC, PolygonStamped, self.trajectory_callback, queue_size=10)
    self.local_sub = rospy.Subscriber(self.LOCALIZATION_TOPIC, PoseStamped, self.localization_callback, queue_size=10)
    self.drive_pub = rospy.Publisher(self.DRIVE_TOPIC, AckermannDriveStamped, queue_size=10)
    self.test_pub = rospy.Publisher('/test_data', String, queue_size = 10)
    self.traj_pub = rospy.Publisher('/pure_pursuit/subsampled', Marker, queue_size=10)

    # Boolean to signal that a trajectory has been received.
    self.has_traj = False

    # List of 2D points along the received trajectory.
    self.traj_pts = []

    # A list of speeds to command between each pair of waypoints.
    self.traj_speeds = []

    # Once the controller seeks the final endpoint, the path is complete.
    self.seeking_end_pt = False
    
    # Store a circular buffer of drive commands for the derivative term.
    self.steer_command_buffer = utils.CircularArray(10)
    for _ in range(10): self.steer_command_buffer.append(0)

    # Init curr pose of vehicle within map frame.
    self.curr_pose = (0, 0)
    self.curr_angle = 0
    self.goal_pose = (0, 0) # Initialize goal pose (set later).

    # Stores index of current pt in the trajectory that vehicle is aiming for.
    self.curr_traj_goal_pt = 0

    # Initialize drive msg.
    self.drive_msg = AckermannDriveStamped()
    self.drive_msg.header.frame_id = "base_link"
    self.drive_msg.drive.steering_angle = 0.0
    self.drive_msg.drive.speed = 0.0
    # Don't limit the steering vel and acceleration of the vehicle.
    self.drive_msg.drive.steering_angle_velocity = 0
    self.drive_msg.drive.acceleration = 0
    self.drive_msg.drive.jerk = 0
    
    self.start_time = 0.0
    self.end_time = 0.0
    
    self.prev_angle_comms = [None]*10
    
    rospy.loginfo('Initialized Pure Pursuit node!')

  def test_cb(self, msg):
    if "DEAD" in msg.data:
      self.has_traj = False

  def trajectory_callback(self, msg):
    """
    Clears the currently followed trajectory, and loads the new one from the message.
    """
    # wait to move until all calculations are finished
    self.has_traj = False
    rospy.loginfo('Received new trajectory with %f points.' % len(msg.polygon.points))

    # Subsample the trajectory.
    self.divideTrajectory(msg.polygon.points)

    # Assign a driving speed between each pair of waypoints.
    self.assignWaypointSpeeds()

    self.publish_trajectory()

    # Start search for new goal pts at beginning of newly defined trajectory.
    self.curr_traj_goal_pt = 0

    # Update boolean states.
    self.has_traj = True
    self.seeking_end_pt = False
    self.start_time = rospy.get_time()
    self.end_time = 0.0
    self.test_pub.publish("PURSUIT START")

  def localization_callback(self, msg):
    """
    msg: (PoseStamped message) the latest inferred pose.
    """
    # rospy.loginfo('Localization callback.')
    self.curr_pose = (msg.pose.position.x, msg.pose.position.y)
    self.curr_angle = utils.quaternion_to_angle(msg.pose.orientation)
    if (self.has_traj):
      self.updateDrive()

  def publishDrive(self, angle, speed):
    """
    Publish drive msg to update steering angle and driving speed.
    """
    slip_speed = max(1.3, -115.37*angle**3 + 107.42*angle**2 - 34.54*angle + 5.86)
    if (self.PERCENT_SLIP_SPEED_INSTANTANEOUS * slip_speed < speed):
      rospy.loginfo("Limiting speed to %f" % (self.PERCENT_SLIP_SPEED_INSTANTANEOUS * slip_speed))
    self.drive_msg.drive.steering_angle = angle
    self.drive_msg.drive.speed = min(speed, self.PERCENT_SLIP_SPEED_INSTANTANEOUS*slip_speed)
    self.drive_pub.publish(self.drive_msg)

  def publish_trajectory(self, duration=0.0, should_publish=True):
    rospy.loginfo('Publishing subsampled trajectory.')
    marker = Marker()
    marker.header = utils.make_header("/map")
    marker.ns = 'visualization/'
    marker.id = 2
    marker.type = 4 # line strip
    marker.lifetime = rospy.Duration.from_sec(duration)
    if should_publish:
      marker.action = 0
      marker.scale.x = 0.1
      marker.scale.y = 0.1
      marker.scale.z = 0.05
      marker.color.r = 0.0
      marker.color.g = 0.0
      marker.color.b = 1.0
      marker.color.a = 0.75
      for p in self.traj_pts:
        pt = Point32()
        pt.x = p[0]
        pt.y = p[1]
        pt.z = -0.1
        marker.points.append(pt)
    else:
      marker.action = 2
    self.traj_pub.publish(marker)
    rospy.loginfo('Publishing a subsampled trajectory with %d points.' % len(marker.points))

  def desiredAngleToSteerAngle(self, dist_to_goal, des_angle):
    """
    Translate the car centroid angle to a drive steering angle.
    """
    return -math.atan2(self.WHEELBASE_LENGTH*math.sin(des_angle), ((dist_to_goal/2.0) + (self.WHEELBASE_LENGTH/2.0)*math.cos(des_angle)))

  def updateDrive(self):
    self.findNextGoalPt()

    # Stop the vehicle if close enough to goal.
    if (self.seeking_end_pt):
      rospy.loginfo('Reached the goal, commmanding zero velocity. Lap Time = %f' % (self.end_time-self.start_time))
      # self.test_pub.publish("PURSUIT DONE.")
      speed = 0
    else:
      speed = self.traj_speeds[self.curr_traj_goal_pt-1]
      # speed = min(abs(self.SPEED_GAIN * self.distToPt(self.goal_pose)), self.MAX_SPEED)

    # determine the correct steering angle
    # calculate angle offset from desired angle to get to goal
    angle_to_goal = math.atan2(self.goal_pose[1]-self.curr_pose[1], self.goal_pose[0]-self.curr_pose[0])
    angle_diff = (angle_to_goal - self.curr_angle)

    if (angle_diff > math.pi):
      angle_diff -= 2*math.pi
    elif (angle_diff < -math.pi):
      angle_diff += 2*math.pi

    des_turn_angle = -angle_diff

    rospy.loginfo('Updating Drive: goal_angle=%f current_angle=%f desired_angle=%f' %
      (angle_to_goal, self.curr_angle, des_turn_angle))

    angle = self.ANGLE_PROP_GAIN * self.desiredAngleToSteerAngle(self.distToPt(self.goal_pose), des_turn_angle)
    prop_gain_term = angle
    
    #if self.prev_angle_comms[0] is not None:
    # angle += -1 * self.ANGLE_DERIV_GAIN * (angle - self.steer_command_buffer.mean()) #(angle-self.prev_angle_comms[0])
    # deriv_gain_term = -1 * self.ANGLE_DERIV_GAIN * (angle - self.steer_command_buffer.mean()) # (angle-self.prev_angle_comms[0])
    deriv_gain_term = 0.0    
    rospy.loginfo("Drive command terms: Pterm=%f Dterm=%f" % (prop_gain_term, deriv_gain_term))
    
    # Limit the steering angle.
    angle_sign = -1 if angle < 0 else 1
    angle = angle_sign * min(abs(angle+0.01), self.MAX_TURN)

    # self.prev_angle_comms = self.prev_angle_comms[1:] + [angle]
    # self.steer_command_buffer.append(angle)

    # Publish the drive msg.
    rospy.loginfo('Drive Command: angle=%f speed=%f' % (angle, speed))
    self.publishDrive(angle + 0.01, speed)

  def findNextGoalPt(self):
    """
    Finds the point on the subsampled trajectory that is closest to the desired
    lookahead distance, and chooses that as a local goal.
    """
    most_recent_speed = self.drive_msg.drive.speed
    lookahead = (most_recent_speed/self.MAX_SPEED) * self.MAX_LOOKAHEAD
    lookahead = min(max(lookahead, self.MIN_LOOKAHEAD), self.MAX_LOOKAHEAD)

    rospy.loginfo('Lookahead Distance: %f' % lookahead)

    # Calculate the closeness of each pt to desired lookahead dist.
    dists = [abs(self.distToPt(pt)-lookahead) for pt in \
      self.traj_pts[self.curr_traj_goal_pt:min(len(self.traj_pts)-1, self.curr_traj_goal_pt + len(self.traj_pts) / 2)]]

    # Find pt closest to lookaheads dist away from remaining pts in trajectory.
    self.curr_traj_goal_pt += np.argmin(dists)
    rospy.loginfo('Current point: %d' % self.curr_traj_goal_pt)
    # rospy.loginfo('Total points: %d', len(self.traj_pts))

    # self.curr_traj_goal_pt += dists.index(min(dists))

    self.goal_pose = self.traj_pts[self.curr_traj_goal_pt]
    # if self.traj_pts[self.curr_traj_goal_pt]
    if (self.curr_traj_goal_pt >= len(self.traj_pts)-5):
      self.seeking_end_pt = True
      if self.end_time == 0.0:
        self.end_time = rospy.get_time()

  def distToPt(self, pt):
    return math.hypot(pt[0] - self.curr_pose[0], pt[1] - self.curr_pose[1])

  def divideTrajectory(self, points):
    """
    Subdivide a list of points.
    points: (list of Point objects)
    """
    self.traj_pts = []

    for index in range(len(points)-1):
      num_subsamples = math.hypot(points[index+1].x - points[index].x,\
        points[index+1].y - points[index].y) / self.TRAJECTORY_SLICE_SIZE

      xs = np.linspace(points[index].x, points[index+1].x, num=num_subsamples)
      ys = np.linspace(points[index].y, points[index+1].y, num=num_subsamples)

      # Don't add the last point, because it will be added as the first point of
      # the next segment.
      self.traj_pts.extend(zip(xs, ys)[0:-1])

    # Add final trajectory point.
    self.traj_pts.append((points[-1].x, points[-1].y))

    # Smooth the trajectory with a moving average.
    # path_as_numpy_array = np.array(self.trajectory.points)
    self.traj_pts = utils.moving_average(np.array(self.traj_pts))
    # self.trajectory.points = [tuple(point) for point in self.smoothed_path]
    # self.trajectory.update_distances()
    # print "Loaded:", len(self.trajectory.points), "smoothed points"
    rospy.loginfo('Finished dividing trajectory (and smoothing).')

  def assignWaypointSpeeds(self):
    """
    Given the current list of trajectory points, determine the best speeds
    to drive at between each of the waypoints.
    """
    rospy.loginfo('Determining driving speeds for trajectory.')
    for i in range(len(self.traj_pts)-2):
      # Find angle that the vehicle will turn when going through the next turn.
      start = self.traj_pts[i]
      pivot = self.traj_pts[i+1]
      end = self.traj_pts[i+2]

      v1 = [pivot[0] - start[0], pivot[1] - start[1]]
      v2 = [end[0] - pivot[0], end[1] - pivot[1]]
      dotnorm = (v1[0]*v2[0] + v1[1]*v2[1]) / (math.hypot(v1[0], v1[1]) * math.hypot(v2[0], v2[1]))
      
      if dotnorm > 1.0:
        dotnorm = 1.0
      elif dotnorm < -1.0:
        dotnorm = -1.0
      theta = math.acos(dotnorm)

      # Calculate wheel slip speed based on turning angle.
      # http://rayban.vision/#doing_donuts_pt1
      slip_speed = max(1.3, -115.37*theta**3 + 107.42*theta**2 - 34.54*theta + 5.86)

      # Travel at some percentage of the slip speed.
      speed = min(self.PERCENT_SLIP_SPEED * slip_speed, self.MAX_SPEED)
      self.traj_speeds.append(speed)

    self.traj_speeds.append(self.traj_speeds[-1])

    # TODO: determine this value.
    # self.ZERO_THROTTLE_DECELERATION = -5.0 # m/sec^2

    # Find any decelerations, and make sure that the deceleration occurs
    # across enough segments of the trajectory.
    smoothed = self.traj_speeds[:]
    for i in range(1, len(self.traj_speeds)):
      if self.traj_speeds[i] < self.traj_speeds[i-1]:
        time_to_decelerate = (self.traj_speeds[i] - self.traj_speeds[i-1]) / self.ZERO_THROTTLE_DECELERATION
        dist_to_decelerate = abs(0.5 * self.ZERO_THROTTLE_DECELERATION * time_to_decelerate**2)
        points_to_decelerate = int(dist_to_decelerate / self.TRAJECTORY_SLICE_SIZE)

        for j in range(max(0, i-points_to_decelerate), i):
          smoothed[j] = self.traj_speeds[i]

    self.traj_speeds = smoothed

if __name__=="__main__":
  rospy.init_node("pure_pursuit")
  pf = PurePursuit()
  rospy.spin()
