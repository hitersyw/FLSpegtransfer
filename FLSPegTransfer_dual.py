import numpy as np
import cv2
import ros_numpy
import rospy
import time
from cv_bridge import CvBridge, CvBridgeError
from sensor_msgs.msg import Image, CompressedImage, PointCloud2
import sys

from FLSpegtransfer.vision.BlockDetection_dual import BlockDetection
from FLSpegtransfer.vision.MappingC2R import MappingC2R
from FLSpegtransfer.motion.dvrkBlockTransfer import dvrkBlockTransfer
import FLSpegtransfer.utils.CmnUtil as U


class FLSPegTransfer():
    def __init__(self):
        # import other modules
        row_board = 6
        col_board = 8
        filename = 'calibration_files/mapping_table_PSM1'
        self.__mapping1 = MappingC2R(filename, row_board, col_board)
        filename = 'calibration_files/mapping_table_PSM2'
        self.__mapping2 = MappingC2R(filename, row_board, col_board)
        self.__block_detection = BlockDetection()
        self.__dvrk = dvrkBlockTransfer()

        # data members
        self.__bridge = CvBridge()
        self.__img_color = []
        self.__img_depth = []
        self.__points_list = []
        self.__points_ros_msg = PointCloud2()

        self.__moving_l2r_flag = True
        self.__moving_r2l_flag = False

        self.__pos_offset1 = [0.0, 0.0, 0.0]    # offset in (m)
        self.__pos_offset2 = [0.0, 0.0, 0.0]

        # ROS subscriber
        rospy.Subscriber('/zivid_camera/color/image_color/compressed', CompressedImage, self.__img_color_cb)
        rospy.Subscriber('/zivid_camera/depth/image_raw', Image, self.__img_depth_cb)
        # rospy.Subscriber('/zivid_camera/points', PointCloud2, self.__pcl_cb)  # not used in this time

        # create ROS node
        if not rospy.get_node_uri():
            rospy.init_node('Image_pipeline_node', anonymous=True, log_level=rospy.WARN)
            print ("ROS node initialized")
        else:
            rospy.logdebug(rospy.get_caller_id() + ' -> ROS already initialized')

        self.interval_ms = 300
        self.rate = rospy.Rate(1000.0 / self.interval_ms)
        self.main()

    def __img_color_cb(self, data):
        try:
            if type(data).__name__ == 'CompressedImage':
                img_raw = self.__compressedimg2cv2(data)
            elif type(data).__name__ == 'Image':
                img_raw = self.__bridge.imgmsg_to_cv2(data, "bgr8")
            self.__img_color = self.__img_crop(img_raw)
        except CvBridgeError as e:
            print(e)

    def __compressedimg2cv2(self, comp_data):
        np_arr = np.fromstring(comp_data.data, np.uint8)
        return cv2.imdecode(np_arr, cv2.IMREAD_COLOR)

    def __img_depth_cb(self, data):
        try:
            if type(data).__name__ == 'CompressedImage':
                img_raw = self.__compressedimg2cv2(data)
            elif type(data).__name__ == 'Image':
                img_raw = self.__bridge.imgmsg_to_cv2(data, "32FC1")
            self.__img_depth = self.__img_crop(img_raw)
        except CvBridgeError as e:
            print(e)

    def __pcl_cb(self, data):
        pc = ros_numpy.numpify(data)
        points = np.zeros((pc.shape[0], pc.shape[1], 3))
        points[:, :, 0] = pc['x']
        points[:, :, 1] = pc['y']
        points[:, :, 2] = pc['z']
        self.__points_list = points

    def __img_crop(self, img):
        # Image cropping
        x = 710; w = 520
        y = 450; h = 400
        cropped = img[y:y + h, x:x + w]
        return cropped

    def select_ordering(self, final_gp_larm, final_gp_rarm, direction):
        """Daniel: select block ordering. Need protection against empty arrays.

        Pegs are numbered from 1 to 12. So, for example, if we are doing r2l, and we have six
        blocks available, then the n_larm and n_rarm could be: [ 8  7 10] and [ 9 12 11], respectively.
        Returns (n_larm,n_rarm), two lists (well, numpy arrays) containing the indices.
        The map function converts n_larm and n_rarm to be lists of integers.
        """
        if direction == 'l2r':
            if len(final_gp_larm) == 0:
                n_larm = []
            else:
                n_larm = np.array(final_gp_larm)[:,0]
                n_larm = map(int, n_larm[n_larm<=6])
            if len(final_gp_rarm) == 0:
                n_rarm = []
            else:
                n_rarm = np.array(final_gp_rarm)[:,0]
                n_rarm = map(int, n_rarm[n_rarm<=6])
        elif direction == 'r2l':
            if len(final_gp_larm) == 0:
                n_larm = []
            else:
                n_larm = np.array(final_gp_larm)[:,0]
                n_larm = map(int, n_larm[n_larm>6])
            if len(final_gp_rarm) == 0:
                n_rarm = []
            else:
                n_rarm = np.array(final_gp_rarm)[:,0]
                n_rarm = map(int, n_rarm[n_rarm>6])

        #print(np.array(final_gp_larm), np.array(final_gp_larm).shape)
        #print(np.array(final_gp_rarm), np.array(final_gp_rarm).shape)
        #print(len(final_gp_larm), final_gp_larm)
        #print(len(final_gp_rarm), final_gp_larm)
        #print(n_larm)
        #print(n_rarm)

        # Daniel: what is this code supposed to be doing? A little bit confused. It's not in the single arm case.
        # From tests, if any of the lists are empty then this will make it [0.0], a bit weird.
        # In downstream code, we iterate through both n_larm and n_rarm so we need them to be same lengths.
        # I think that's what this does -- makes lengths the same.
        n_larm = np.pad(n_larm, pad_width=(0, max(0, len(n_rarm)-len(n_larm))), mode='constant', constant_values=(0,0))
        n_rarm = np.pad(n_rarm, pad_width=(0, max(0, len(n_larm)-len(n_rarm))), mode='constant', constant_values=(0,0))

        return n_larm, n_rarm

    def move_blocks(self, pick_number_larm, pick_number_rarm, final_gp_larm, final_pp_larm, final_gp_rarm, final_pp_rarm, direction):
        """Daniel: adding direction parameter.

        Also, some logic in case we have uneven number of blocks per arm, to protect against failures.
        The final_{g,p}p_{l,r}arm values are lists, of length equal to the number of blocks 'assigned' to the arm.
        So for the dual case, it would be a value in {0,1,2,3}.

        HUGE NOTE! The left arm is left w.r.t. us siting not where my machine is but where davinci arm/endo
        machines are if the monitors aren't turned. Thus, LEFT ARM IS PSM2. RIGHT ARM IS PSM1.

        :pick_number_larm: (integer) index of block to pick.
        :pick_number_rarm: (integer) index of block to pick.
        """
        print('\nCalling move_blocks()')
        print('\tpick_number_larm: {}'.format(pick_number_larm))
        print('\tpick_number_rarm: {}'.format(pick_number_rarm))
        print('\tleft arm grasp/pick:  {}, {}'.format(final_gp_larm, final_pp_larm))
        print('\tright arm grasp/pick: {}, {}\n'.format(final_gp_rarm, final_pp_rarm))
        
        # Daniel: needed this check, otherwise final_gp_rarm might be empty and the np.argwhere doesn't apply.
        # This is the right arm, or PSM1, the one that is closer to Daniel's machine. THIS IS THE ONE
        # THAT WE WERE USING FOR THE SINGLE ARM CASE, HENCE WE SHOULD BORROW OLDER CALIBRATION.
        # Oh, also need a check for pick_number_rarm... in case we had something like 3 vs 2 to start.
        if len(final_gp_rarm) == 0 or (pick_number_rarm == 0):
            arg_pick = []
            arg_place = []
            ignore_psm1 = True
        else:
            arg_pick = np.argwhere(np.array(final_gp_rarm)[:, 0] == pick_number_rarm)
            arg_place = arg_pick
            ignore_psm1 = False
            
        if len(arg_pick) == 0 or len(arg_place) == 0:
            pos_pick1 = []
            rot_pick1 = []
            pos_place1 = []
            rot_place1 = []
        else:
            arg_pick = arg_pick[0][0]
            arg_place = arg_place[0][0]
            pos_pick1 = self.__mapping1.transform_pixel2robot(final_gp_rarm[arg_pick][3:], final_gp_rarm[arg_pick][2])
            rot_pick1 = [final_gp_rarm[arg_pick][2], 30.0, -10.0]   # Daniel: yaw/pitch used for PSM1 calibration (what we did in single arm).
            pos_place1 = self.__mapping1.transform_pixel2robot(final_pp_rarm[arg_place][3:], final_pp_rarm[arg_place][2])
            # Daniel: need to tune rot_place1, like in the single-arm case.
            # Since this is PSM1, I am just copying the value we did in single arm.
            # EDIT: all right, sometimes it is not ideal. Darn... may have to re-tune.
            if direction == 'l2r':
                rot_place1 = [final_pp_rarm[arg_place][2], 50.0, -30.0]
            else:
                rot_place1 = [final_pp_rarm[arg_place][2], 50.0, -50.0]

        # Daniel: same thing applies for the other arm!
        if len(final_gp_larm) == 0 or (pick_number_larm == 0):
            arg_pick = []
            arg_place = []
            ignore_psm2 = True
        else:
            arg_pick = np.argwhere(np.array(final_gp_larm)[:, 0] == pick_number_larm)
            arg_place = arg_pick
            ignore_psm2 = False

        if len(arg_pick) == 0 or len(arg_place) == 0:
            pos_pick2 = []
            rot_pick2 = []
            pos_place2 = []
            rot_place2 = []
        else:
            arg_pick = arg_pick[0][0]
            arg_place = arg_place[0][0]
            pos_pick2 = self.__mapping2.transform_pixel2robot(final_gp_larm[arg_pick][3:], final_gp_larm[arg_pick][2])
            rot_pick2 = [final_gp_larm[arg_pick][2], -15.0, 0.0]   # Daniel:  yaw/pitch used for PSM2 calibration.
            pos_place2 = self.__mapping2.transform_pixel2robot(final_pp_larm[arg_place][3:], final_pp_larm[arg_place][2])
            # Daniel: need to tune rot_place2, like in the single-arm case.
            # It probably should not be the same as the PSM1 case.
            # This is the one with the 'slightly broken' arm, FYI.
            if direction == 'l2r':
                rot_place2 = [final_pp_larm[arg_place][2], -30.0, 0.0]
            else:
                rot_place2 = [final_pp_larm[arg_place][2], -20.0, 0.0]

        assert not (ignore_psm1 and ignore_psm2)
        if ignore_psm1:
            which_arm = 'PSM2'
            pos_pick2 = [pos_pick2[0] + self.__pos_offset2[0], pos_pick2[1] + self.__pos_offset2[1],
                         pos_pick2[2] + self.__pos_offset2[2]]
        elif ignore_psm2:
            which_arm = 'PSM1'
            pos_pick1 = [pos_pick1[0] + self.__pos_offset1[0], pos_pick1[1] + self.__pos_offset1[1],
                         pos_pick1[2] + self.__pos_offset1[2]]
        else:
            which_arm = 'Both'
            pos_pick1 = [pos_pick1[0] + self.__pos_offset1[0], pos_pick1[1] + self.__pos_offset1[1],
                         pos_pick1[2] + self.__pos_offset1[2]]
            pos_pick2 = [pos_pick2[0] + self.__pos_offset2[0], pos_pick2[1] + self.__pos_offset2[1],
                         pos_pick2[2] + self.__pos_offset2[2]]
        
        # Finally, call the picking and placing methods.
        self.__dvrk.pickup(pos_pick1=pos_pick1, rot_pick1=rot_pick1, pos_pick2=pos_pick2, rot_pick2=rot_pick2, which_arm=which_arm)
        self.__dvrk.place(pos_place1=pos_place1, rot_place1=rot_place1, pos_place2=pos_place2, rot_place2=rot_place2, which_arm=which_arm)

    def main(self):
        try:
            #user_input = raw_input("Are you going to proceed automatically? (y or n)")
            user_input = 'n'
            if user_input == "y":   auto_flag = True
            elif user_input == "n": auto_flag = False
            else:   return
            while True:
                if self.__img_color == [] or self.__img_depth == []:
                    pass
                else:
                    # Scanning
                    self.__dvrk.move_origin()
                    time.sleep(0.3)

                    # Perception output
                    final_gp_larm, final_pp_larm, final_gp_rarm, final_pp_rarm, peg_points, pegs_overlayed, blocks_overlayed\
                        = self.__block_detection.FLSPerception(self.__img_depth)
                    print('\nJust ran FLSPerception() code to get grasping and placing poses.')
                    print('len(final_gp_larm): {}'.format(len(final_gp_larm)))
                    print('len(final_gp_rarm): {}'.format(len(final_gp_rarm)))

                    cv2.imshow("img_color", self.__img_color)
                    cv2.imshow("masked_pegs", pegs_overlayed)
                    cv2.imshow("masked_blocks", blocks_overlayed)
                    cv2.waitKey(1000)
                    if not auto_flag:
                        user_input = raw_input("1: Left to right,  2: Right to left, anything else quit: ")
                        if user_input == "1":
                            self.__moving_l2r_flag = True
                            self.__moving_r2l_flag = False
                        elif user_input == "2":
                            self.__moving_l2r_flag = False
                            self.__moving_r2l_flag = True
                        else:
                            self.__moving_l2r_flag = False
                            self.__moving_r2l_flag = False
                            print('Exiting now.')
                            sys.exit()

                    # Move blocks from left to right
                    if self.__moving_l2r_flag:
                        n_larm, n_rarm = self.select_ordering(final_gp_larm, final_gp_rarm, direction='l2r')
                        if auto_flag:
                            if len(n_larm)==0 and len(n_rarm)==0:
                                self.__moving_l2r_flag = False
                                self.__moving_r2l_flag = True
                        for nl, nr in zip(n_larm, n_rarm):
                            self.move_blocks(nl, nr, final_gp_larm, final_pp_larm, final_gp_rarm, final_pp_rarm, 'l2r')

                    # Move blocks from right to left
                    elif self.__moving_r2l_flag:
                        n_larm, n_rarm = self.select_ordering(final_gp_larm, final_gp_rarm, direction='r2l')
                        if auto_flag:
                            if len(n_larm)==0 and len(n_rarm)==0:
                                self.__moving_l2r_flag = False
                                self.__moving_r2l_flag = False
                        for nl, nr in zip(n_larm, n_rarm):
                            self.move_blocks(nl, nr, final_gp_larm, final_pp_larm, final_gp_rarm, final_pp_rarm, 'r2l')
        finally:
            cv2.destroyAllWindows()

if __name__ == '__main__':
    FLSPegTransfer()
