import datetime
import os
import select
import shutil
import socket
import time
import json

import cv2

import botDetector
from OverheadCamera import OverheadCamera as oc
from botDetector import *


def getPitch():
    mag_x, mag_y, mag_z = sensor.magnetic
    pitch_rad = math.atan2(mag_y, math.sqrt(mag_x ** 2 + mag_z ** 2))
    pitch_deg = math.degrees(pitch_rad) % 360

    return pitch_deg


# Run parameters
RUN_SERVER = True  # Will run a server and wait for a client connection if True
IS_RPI = False  # Set to True for the Raspberry Pi, False to test on a Windows computer
DISPLAY = True # Will only open a window to view the camera frames if this is True
SAVE_FRAME_RATE = 0  # Frame rate to save captured images for later viewing. Will not save if set to 0 or negative.
HAS_COMPASS = False  # If True, will attempt to use a magnetometer to find the compass heading of the field's major axis
RUN_DETECTION = True    # If True, will execute robot detection algorithm. If not, will

PACKET_SIZE = 1024


def configDataPacket():

    data_dict = {
        'FPS': SAVE_FRAME_RATE,
        'PACKET_SIZE': PACKET_SIZE
    }

    data = json.dumps(data_dict)

    return data


#  Define a function to perform the contour detection
def getAllContours(grayscale_img):
    canny = cv2.Canny(grayscale_img, 50, 240)
    contours, _ = cv2.findContours(canny, cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
    return contours


def makeVideo(name, image_folder):
    images = [img for img in sorted(os.listdir(image_folder)) if img.endswith('.jpg')]
    if len(images) < 1:
        return

    frame = cv2.imread(os.path.join(image_folder, images[0]))
    height, width, layers = frame.shape

    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    video = cv2.VideoWriter(name + '.mp4', fourcc, SAVE_FRAME_RATE, (width, height))

    for image in images:
        video.write(cv2.imread(os.path.join(image_folder, image)))

    video.release()


CAM_WIDTH = 4656  # Width of the camera frame in pixels
CAM_HEIGHT = 3496  # Height of the camera frame in pixels
CAM_FOV_WIDTH = 110  # Width of the camera frame in degrees, also called horizontal field of view
CAM_FOV_HEIGHT = 95  # Height of the camera frame in degrees, also called vertical field of view

# Configure the camera
if IS_RPI:

    # Pi-only module for operating the camera
    # picamera2 does not need to be installed to run on a non-RPi system
    from picamera2 import Picamera2

    CAM_WIDTH = 4656
    CAM_HEIGHT = 3496
    CAM_FOV_WIDTH = 110
    CAM_FOV_HEIGHT = 95

    # Set up the Raspberry Pi webcam
    picam2 = Picamera2()
    picam2.configure(picam2.create_preview_configuration(main={'format': 'XRGB8888', 'size': (CAM_WIDTH, CAM_HEIGHT)}))

    picam2.start()
    print('Configuring exposure...')
    exposure = picam2.capture_metadata()['ExposureTime']
    print(exposure)

    # Factor to adjust exposure time
    # 1/12 seems to work
    exposure_factor = 1 / 12

    # Stop the webcam and reduce exposure time, then restart the webcam
    picam2.stop()
    picam2.set_controls({'ExposureTime': int(exposure * exposure_factor)})

    picam2.start()

    if HAS_COMPASS:
        # Pi-only imports to operate the magnetometer (digital compass)
        import adafruit_lis3mdl
        import board
        import busio
        import math

        # Configure the magnetometer
        i2c = busio.I2C(board.SCL, board.SDA)
        sensor = adafruit_lis3mdl.LIS3MDL(i2c)

else:

    # Typical settings for a widescreen laptop webcam
    CAM_WIDTH = 1280
    CAM_HEIGHT = 720
    CAM_FOV_WIDTH = 65
    CAM_FOV_HEIGHT = 37

    # Limit the camera exposure to detect LEDs while filtering out other light sources
    # -8 seems to work for testing
    exposure_factor = 0#-8

    # Set up the default Windows webcam
    vid = cv2.VideoCapture(0)
    vid.set(cv2.CAP_PROP_FRAME_WIDTH, CAM_WIDTH)
    vid.set(cv2.CAP_PROP_FRAME_HEIGHT, CAM_HEIGHT)
    vid.set(cv2.CAP_PROP_EXPOSURE, exposure_factor)

    if not vid.isOpened():
        print('Cannot open camera...')
        exit()

# Define the overhead camera object that performs coordinate transformations
# Use feet for the height and offset measurements to ensure the output of the algorithm is also in feet
cam = oc(
    field_of_view=(CAM_FOV_WIDTH, CAM_FOV_HEIGHT),
    phi=90,
    image_size=(CAM_WIDTH, CAM_HEIGHT),
    midfield_offset=0,
    sideline_offset=0,
    height=19+8/12,
    bot_height=1+10/12
)

# Start the TCP server
if RUN_SERVER:
    print('Starting server...')

    host = socket.gethostname()
    port = 5000

    # Open the TCP socket at the given port
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.bind(('', port))
    server_socket.listen(1)
    print('Listening for TCP session requests at ' + host + ':' + str(port))

    conn, address = server_socket.accept()
    print('Accepting TCP session from ' + str(address))

    conn.send(configDataPacket().encode())


def main():
    # Get a unique name for the recording of the session
    session_name = 'recording_' + datetime.datetime.now().strftime('%Y_%m_%d__%H_%M_%S')

    # Determine whether to record the session based on the given frame rate
    record = False
    frame_interval = math.inf
    if SAVE_FRAME_RATE > 0:
        frame_interval = 1 / SAVE_FRAME_RATE
        record = True

        # If the application needs to record, make an empty folder in which to save images
        if os.path.exists(session_name):
            shutil.rmtree(session_name)
        os.makedirs(session_name)

    # Get the start time of the session
    start = time.time()
    mark = start

    # Main loop
    while True:

        # Capture a frame from the webcam
        if IS_RPI:
            frame = picam2.capture_array()
        else:
            _, frame = vid.read()

        # If q is pressed, stop the main loop
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

        # Convert frame to grayscale
        gray_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        # Identify bright spots in the image such as LEDs and put them in a binary image
        _, binary_m = cv2.threshold(gray_frame, 230, 255, cv2.THRESH_BINARY)

        # Get all contours (boundaries of the white spots) in the binary image
        contours = getAllContours(binary_m)

        # Get a list of the center points of all LEDs
        LEDs = []

        # Print each contour on the original frame
        for contour in contours:

            # Get the center point of the contour
            M = cv2.moments(contour)
            if M["m00"] != 0 and cv2.contourArea(contour) > 1:
                # Draw the contour
                cv2.drawContours(frame, [contour], -1, (255, 255, 0), 3)

                # Find and draw the contour's center
                cX = int(M['m10'] / M['m00'])
                cY = int(M['m01'] / M['m00'])

                cv2.circle(frame, (cX, cY), 4, (0, 255, 255), -1)

                # Convert the pixel coordinates to field coordinates
                x, y, z = cam.pixelsToCartesian(cX, cY)
                LEDs.append((x, y))

                cv2.putText(frame, 'LED position: {:.2f}, {:.2f}'.format(x, y), (cX, cY), cv2.FONT_HERSHEY_PLAIN, 1,
                            (0, 255, 0), 2, cv2.LINE_AA)

        if record:
            # If the current time exceeds the time at which the next frame should be captured, save the current frame
            now = time.time()
            if now - mark >= 0:
                mark = mark + frame_interval

                cv2.imwrite(session_name + '/' + str(now) + '.jpg', frame)

        bots_in_play = ('X', 'Y', 'STAIR', 'H', 'L')
        bot_positions = {
            'CAM': (cam.x_offset + oc.FIELD_LENGTH, cam.y_offset + oc.FIELD_WIDTH)
        }

        groups = botDetector.groupNearbyPoints(LEDs, 1)

        for bot_pattern in bots_in_play:
            best_score = math.inf
            best_bot = None

            for group in groups:

                if len(group) < 1:
                    continue

                score = botDetector.detectShape(group, botPatterns.getPattern(bot_pattern))
                if score < best_score:
                    best_bot = group
                    best_score = score
                    print('Matching score: ' + str(score))

            bot_position = botDetector.groupCenter(best_bot)
            bot_positions[bot_pattern] = bot_position

        if HAS_COMPASS:
            angle = getPitch()
            print('Compass heading: ' + str(angle))

        # If the server is running, transmit the points to the client
        if RUN_SERVER:
            try:
                data = json.dumps(bot_positions)

                print('Sending ' + data)
                conn.send(data.encode())

                conn.recv(PACKET_SIZE).decode()
            except BrokenPipeError:
                print("The client suddenly closed. Continuing to listen...")

        if DISPLAY:
            cv2.imshow('frame', frame)

    # Close the TCP socket
    if RUN_SERVER:
        conn.close()
        print('TCP socket closed...')

    # Destroy the display window for the live view
    if DISPLAY:
        cv2.destroyAllWindows()

    if IS_RPI:
        # Stop recording from the Pi's camera
        picam2.stop()
    else:
        # Stop recording from the Windows camera
        vid.release()

    # Convert the saved images to a video
    if record:
        makeVideo(name=session_name, image_folder=session_name)


if __name__ == '__main__':
    main()

