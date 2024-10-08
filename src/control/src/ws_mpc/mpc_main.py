#!/usr/bin/env python3
"""

Path tracking simulation with iterative linear model predictive control for speed and steer control

author: Li Xingyou

Run MPC_Controller.run
Get waypoint from load_arrays_from_file

"""
import matplotlib.pyplot as plt
import time
import cvxpy
import math
import numpy as np
import sys
from simple_pid import PID

from scipy.spatial.transform import Rotation as Rot
import pathlib
sys.path.append(str(pathlib.Path(__file__).parent.parent.parent))

from pyproj import Transformer
import bisect
from geometry_msgs.msg import Twist
from transforms3d.euler import quat2euler

from ros_tools import *

NX = 4  # x = x, y, v, yaw
NU = 2  # a = [accel, steer]
T = 5  # horizon length

# mpc parameters
R = np.diag([0.01, 0.01])  # input cost matrix
Rd = np.diag([0.01, 1.0])  # input difference cost matrix
Q = np.diag([1.0, 1.0, 0.5, 0.5])  # state cost matrix
Qf = Q  # state final matrix
GOAL_DIS = 1.5  # goal distance
STOP_SPEED = 0.5 / 3.6  # stop speed
MAX_TIME = 500.0  # max simulation time

# iterative paramter
MAX_ITER = 3  # Max iteration
DU_TH = 0.1  # iteration finish param

TARGET_SPEED = 15.0 / 3.6  # [m/s] target speed
N_IND_SEARCH = 10  # Search index number

DT = 0.2  # [s] time tick

# Vehicle parameters
LENGTH = 4.5  # [m]
WIDTH = 2.0  # [m]
BACKTOWHEEL = 1.0  # [m]
WHEEL_LEN = 0.3  # [m]
WHEEL_WIDTH = 0.2  # [m]
TREAD = 0.7  # [m]
WB = 2.5  # [m]

MAX_STEER = np.deg2rad(45.0)  # maximum steering angle [rad]
MAX_DSTEER = np.deg2rad(30.0)  # maximum steering speed [rad/s]
MAX_SPEED = 55.0 / 3.6  # maximum speed [m/s]
MIN_SPEED = 0.0 / 3.6  # minimum speed [m/s]
MAX_ACCEL = 1.0  # maximum accel [m/ss]

show_animation = True


class GNSStoUTMConverter:
    def __init__(self):
        # Initialize the converter
        pass

    def determine_utm_zone(self, lon):
        # Determine the UTM zone based on longitude
        return int((lon + 180) / 6) + 1

    def convert(self, lat, lon):
        # Convert GNSS coordinates to UTM
        zone_number = self.determine_utm_zone(lon)
        
        # Create a transformer object
        transformer = Transformer.from_crs(
            "EPSG:4326",  # WGS84 GNSS coordinate system
            f"+proj=utm +zone={zone_number} +datum=WGS84",  # UTM coordinate system
            always_xy=True
        )
        
        # Perform the transformation
        easting, northing = transformer.transform(lon, lat)
        
        return easting, northing, zone_number

class CubicSpline1D:
    """
    1D Cubic Spline class

    Parameters
    ----------
    x : list
        x coordinates for data points. This x coordinates must be
        sorted
        in ascending order.
    y : list
        y coordinates for data points

    Examples
    --------
    You can interpolate 1D data points.

    >>> import numpy as np
    >>> import matplotlib.pyplot as plt
    >>> x = np.arange(5)
    >>> y = [1.7, -6, 5, 6.5, 0.0]
    >>> sp = CubicSpline1D(x, y)
    >>> xi = np.linspace(0.0, 5.0)
    >>> yi = [sp.calc_position(x) for x in xi]
    >>> plt.plot(x, y, "xb", label="Data points")
    >>> plt.plot(xi, yi , "r", label="Cubic spline interpolation")
    >>> plt.grid(True)
    >>> plt.legend()
    >>> plt.show()

    .. image:: cubic_spline_1d.png

    """

    def __init__(self, x, y):

        h = np.diff(x)
        if np.any(h < 0):
            raise ValueError("x coordinates must be sorted in ascending order")

        self.a, self.b, self.c, self.d = [], [], [], []
        self.x = x
        self.y = y
        self.nx = len(x)  # dimension of x

        # calc coefficient a
        self.a = [iy for iy in y]

        # calc coefficient c
        A = self.__calc_A(h)
        B = self.__calc_B(h, self.a)
        self.c = np.linalg.solve(A, B)

        # calc spline coefficient b and d
        for i in range(self.nx - 1):
            d = (self.c[i + 1] - self.c[i]) / (3.0 * h[i])
            b = 1.0 / h[i] * (self.a[i + 1] - self.a[i]) \
                - h[i] / 3.0 * (2.0 * self.c[i] + self.c[i + 1])
            self.d.append(d)
            self.b.append(b)

    def calc_position(self, x):
        """
        Calc `y` position for given `x`.

        if `x` is outside the data point's `x` range, return None.

        Returns
        -------
        y : float
            y position for given x.
        """
        if x < self.x[0]:
            return None
        elif x > self.x[-1]:
            return None

        i = self.__search_index(x)
        dx = x - self.x[i]
        position = self.a[i] + self.b[i] * dx + \
            self.c[i] * dx ** 2.0 + self.d[i] * dx ** 3.0

        return position

    def calc_first_derivative(self, x):
        """
        Calc first derivative at given x.

        if x is outside the input x, return None

        Returns
        -------
        dy : float
            first derivative for given x.
        """

        if x < self.x[0]:
            return None
        elif x > self.x[-1]:
            return None

        i = self.__search_index(x)
        dx = x - self.x[i]
        dy = self.b[i] + 2.0 * self.c[i] * dx + 3.0 * self.d[i] * dx ** 2.0
        return dy

    def calc_second_derivative(self, x):
        """
        Calc second derivative at given x.

        if x is outside the input x, return None

        Returns
        -------
        ddy : float
            second derivative for given x.
        """

        if x < self.x[0]:
            return None
        elif x > self.x[-1]:
            return None

        i = self.__search_index(x)
        dx = x - self.x[i]
        ddy = 2.0 * self.c[i] + 6.0 * self.d[i] * dx
        return ddy

    def __search_index(self, x):
        """
        search data segment index
        """
        return bisect.bisect(self.x, x) - 1

    def __calc_A(self, h):
        """
        calc matrix A for spline coefficient c
        """
        A = np.zeros((self.nx, self.nx))
        A[0, 0] = 1.0
        for i in range(self.nx - 1):
            if i != (self.nx - 2):
                A[i + 1, i + 1] = 2.0 * (h[i] + h[i + 1])
            A[i + 1, i] = h[i]
            A[i, i + 1] = h[i]

        A[0, 1] = 0.0
        A[self.nx - 1, self.nx - 2] = 0.0
        A[self.nx - 1, self.nx - 1] = 1.0
        return A

    def __calc_B(self, h, a):
        """
        calc matrix B for spline coefficient c
        """
        B = np.zeros(self.nx)
        for i in range(self.nx - 2):
            B[i + 1] = 3.0 * (a[i + 2] - a[i + 1]) / h[i + 1]\
                - 3.0 * (a[i + 1] - a[i]) / h[i]
        return B

class CubicSpline2D:
    """
    Cubic CubicSpline2D class

    Parameters
    ----------
    x : list
        x coordinates for data points.
    y : list
        y coordinates for data points.

    Examples
    --------
    You can interpolate a 2D data points.

    >>> import matplotlib.pyplot as plt
    >>> x = [-2.5, 0.0, 2.5, 5.0, 7.5, 3.0, -1.0]
    >>> y = [0.7, -6, 5, 6.5, 0.0, 5.0, -2.0]
    >>> ds = 0.1  # [m] distance of each interpolated points
    >>> sp = CubicSpline2D(x, y)
    >>> s = np.arange(0, sp.s[-1], ds)
    >>> rx, ry, ryaw, rk = [], [], [], []
    >>> for i_s in s:
    ...     ix, iy = sp.calc_position(i_s)
    ...     rx.append(ix)
    ...     ry.append(iy)
    ...     ryaw.append(sp.calc_yaw(i_s))
    ...     rk.append(sp.calc_curvature(i_s))
    >>> plt.subplots(1)
    >>> plt.plot(x, y, "xb", label="Data points")
    >>> plt.plot(rx, ry, "-r", label="Cubic spline path")
    >>> plt.grid(True)
    >>> plt.axis("equal")
    >>> plt.xlabel("x[m]")
    >>> plt.ylabel("y[m]")
    >>> plt.legend()
    >>> plt.show()

    .. image:: cubic_spline_2d_path.png

    >>> plt.subplots(1)
    >>> plt.plot(s, [np.rad2deg(iyaw) for iyaw in ryaw], "-r", label="yaw")
    >>> plt.grid(True)
    >>> plt.legend()
    >>> plt.xlabel("line length[m]")
    >>> plt.ylabel("yaw angle[deg]")

    .. image:: cubic_spline_2d_yaw.png

    >>> plt.subplots(1)
    >>> plt.plot(s, rk, "-r", label="curvature")
    >>> plt.grid(True)
    >>> plt.legend()
    >>> plt.xlabel("line length[m]")
    >>> plt.ylabel("curvature [1/m]")

    .. image:: cubic_spline_2d_curvature.png
    """

    def __init__(self, x, y):
        self.s = self.__calc_s(x, y)
        self.sx = CubicSpline1D(self.s, x)
        self.sy = CubicSpline1D(self.s, y)

    def __calc_s(self, x, y):
        dx = np.diff(x)
        dy = np.diff(y)
        self.ds = np.hypot(dx, dy)
        s = [0]
        s.extend(np.cumsum(self.ds))
        return s

    def calc_position(self, s):
        """
        calc position

        Parameters
        ----------
        s : float
            distance from the start point. if `s` is outside the data point's
            range, return None.

        Returns
        -------
        x : float
            x position for given s.
        y : float
            y position for given s.
        """
        x = self.sx.calc_position(s)
        y = self.sy.calc_position(s)

        return x, y

    def calc_curvature(self, s):
        """
        calc curvature

        Parameters
        ----------
        s : float
            distance from the start point. if `s` is outside the data point's
            range, return None.

        Returns
        -------
        k : float
            curvature for given s.
        """
        dx = self.sx.calc_first_derivative(s)
        ddx = self.sx.calc_second_derivative(s)
        dy = self.sy.calc_first_derivative(s)
        ddy = self.sy.calc_second_derivative(s)
        k = (ddy * dx - ddx * dy) / ((dx ** 2 + dy ** 2)**(3 / 2))
        return k

    def calc_yaw(self, s):
        """
        calc yaw

        Parameters
        ----------
        s : float
            distance from the start point. if `s` is outside the data point's
            range, return None.

        Returns
        -------
        yaw : float
            yaw angle (tangent vector) for given s.
        """
        dx = self.sx.calc_first_derivative(s)
        dy = self.sy.calc_first_derivative(s)
        yaw = math.atan2(dy, dx)
        return yaw

def calc_spline_course(x, y, ds=0.1):
    sp = CubicSpline2D(x, y)
    s = list(np.arange(0, sp.s[-1], ds))

    rx, ry, ryaw, rk = [], [], [], []
    for i_s in s:
        ix, iy = sp.calc_position(i_s)
        rx.append(ix)
        ry.append(iy)
        ryaw.append(sp.calc_yaw(i_s))
        rk.append(sp.calc_curvature(i_s))

    return rx, ry, ryaw, rk, s

def angle_mod(x, zero_2_2pi=False, degree=False):
    """
    Angle modulo operation
    Default angle modulo range is [-pi, pi)

    Parameters
    ----------
    x : float or array_like
        A angle or an array of angles. This array is flattened for
        the calculation. When an angle is provided, a float angle is returned.
    zero_2_2pi : bool, optional
        Change angle modulo range to [0, 2pi)
        Default is False.
    degree : bool, optional
        If True, then the given angles are assumed to be in degrees.
        Default is False.

    Returns
    -------
    ret : float or ndarray
        an angle or an array of modulated angle.

    Examples
    --------
    >>> angle_mod(-4.0)
    2.28318531

    >>> angle_mod([-4.0])
    np.array(2.28318531)

    >>> angle_mod([-150.0, 190.0, 350], degree=True)
    array([-150., -170.,  -10.])

    >>> angle_mod(-60.0, zero_2_2pi=True, degree=True)
    array([300.])

    """
    if isinstance(x, float):
        is_float = True
    else:
        is_float = False

    x = np.asarray(x).flatten()
    if degree:
        x = np.deg2rad(x)

    if zero_2_2pi:
        mod_angle = x % (2 * np.pi)
    else:
        mod_angle = (x + np.pi) % (2 * np.pi) - np.pi

    if degree:
        mod_angle = np.rad2deg(mod_angle)

    if is_float:
        return mod_angle.item()
    else:
        return mod_angle

class State:
    """
    vehicle state class
    """

    def __init__(self, x=0.0, y=0.0, yaw=0.0, v=0.0):
        self.x = x
        self.y = y
        self.yaw = yaw
        self.v = v
        self.predelta = None

def pi_2_pi(angle):
    return angle_mod(angle)

def get_linear_model_matrix(v, phi, delta):

    A = np.zeros((NX, NX))
    A[0, 0] = 1.0
    A[1, 1] = 1.0
    A[2, 2] = 1.0
    A[3, 3] = 1.0
    A[0, 2] = DT * math.cos(phi)
    A[0, 3] = - DT * v * math.sin(phi)
    A[1, 2] = DT * math.sin(phi)
    A[1, 3] = DT * v * math.cos(phi)
    A[3, 2] = DT * math.tan(delta) / WB

    B = np.zeros((NX, NU))
    B[2, 0] = DT
    B[3, 1] = DT * v / (WB * math.cos(delta) ** 2)

    C = np.zeros(NX)
    C[0] = DT * v * math.sin(phi) * phi
    C[1] = - DT * v * math.cos(phi) * phi
    C[3] = - DT * v * delta / (WB * math.cos(delta) ** 2)

    return A, B, C

def plot_car(x, y, yaw, steer=0.0, cabcolor="-r", truckcolor="-k"):  # pragma: no cover

    outline = np.array([[-BACKTOWHEEL, (LENGTH - BACKTOWHEEL), (LENGTH - BACKTOWHEEL), -BACKTOWHEEL, -BACKTOWHEEL],
                        [WIDTH / 2, WIDTH / 2, - WIDTH / 2, -WIDTH / 2, WIDTH / 2]])

    fr_wheel = np.array([[WHEEL_LEN, -WHEEL_LEN, -WHEEL_LEN, WHEEL_LEN, WHEEL_LEN],
                         [-WHEEL_WIDTH - TREAD, -WHEEL_WIDTH - TREAD, WHEEL_WIDTH - TREAD, WHEEL_WIDTH - TREAD, -WHEEL_WIDTH - TREAD]])

    rr_wheel = np.copy(fr_wheel)

    fl_wheel = np.copy(fr_wheel)
    fl_wheel[1, :] *= -1
    rl_wheel = np.copy(rr_wheel)
    rl_wheel[1, :] *= -1

    Rot1 = np.array([[math.cos(yaw), math.sin(yaw)],
                     [-math.sin(yaw), math.cos(yaw)]])
    Rot2 = np.array([[math.cos(steer), math.sin(steer)],
                     [-math.sin(steer), math.cos(steer)]])

    fr_wheel = (fr_wheel.T.dot(Rot2)).T
    fl_wheel = (fl_wheel.T.dot(Rot2)).T
    fr_wheel[0, :] += WB
    fl_wheel[0, :] += WB

    fr_wheel = (fr_wheel.T.dot(Rot1)).T
    fl_wheel = (fl_wheel.T.dot(Rot1)).T

    outline = (outline.T.dot(Rot1)).T
    rr_wheel = (rr_wheel.T.dot(Rot1)).T
    rl_wheel = (rl_wheel.T.dot(Rot1)).T

    outline[0, :] += x
    outline[1, :] += y
    fr_wheel[0, :] += x
    fr_wheel[1, :] += y
    rr_wheel[0, :] += x
    rr_wheel[1, :] += y
    fl_wheel[0, :] += x
    fl_wheel[1, :] += y
    rl_wheel[0, :] += x
    rl_wheel[1, :] += y

    plt.plot(np.array(outline[0, :]).flatten(),
             np.array(outline[1, :]).flatten(), truckcolor)
    plt.plot(np.array(fr_wheel[0, :]).flatten(),
             np.array(fr_wheel[1, :]).flatten(), truckcolor)
    plt.plot(np.array(rr_wheel[0, :]).flatten(),
             np.array(rr_wheel[1, :]).flatten(), truckcolor)
    plt.plot(np.array(fl_wheel[0, :]).flatten(),
             np.array(fl_wheel[1, :]).flatten(), truckcolor)
    plt.plot(np.array(rl_wheel[0, :]).flatten(),
             np.array(rl_wheel[1, :]).flatten(), truckcolor)
    plt.plot(x, y, "*")

def update_state(state, a, delta):

    # input check
    if delta >= MAX_STEER:
        delta = MAX_STEER
    elif delta <= -MAX_STEER:
        delta = -MAX_STEER

    state.x = state.x + state.v * math.cos(state.yaw) * DT
    state.y = state.y + state.v * math.sin(state.yaw) * DT
    state.yaw = state.yaw + state.v / WB * math.tan(delta) * DT
    state.v = state.v + a * DT

    if state.v > MAX_SPEED:
        state.v = MAX_SPEED
    elif state.v < MIN_SPEED:
        state.v = MIN_SPEED

    return state

def update_state_carla(state, easting, northing, yaw_radians_lib, speed):

    # input check
    if yaw_radians_lib >= MAX_STEER:
        yaw_radians_lib = MAX_STEER
    elif yaw_radians_lib <= -MAX_STEER:
        yaw_radians_lib = -MAX_STEER

    state.x = easting
    state.y = northing
    state.yaw = yaw_radians_lib
    state.v = speed

    if state.v > MAX_SPEED:
        state.v = MAX_SPEED
    elif state.v < MIN_SPEED:
        state.v = MIN_SPEED

    return state

def get_nparray_from_matrix(x):
    return np.array(x).flatten()

def calc_nearest_index(state, cx, cy, cyaw, pind):

    dx = [state.x - icx for icx in cx[pind:(pind + N_IND_SEARCH)]]
    dy = [state.y - icy for icy in cy[pind:(pind + N_IND_SEARCH)]]

    d = [idx ** 2 + idy ** 2 for (idx, idy) in zip(dx, dy)]

    mind = min(d)

    ind = d.index(mind) + pind

    mind = math.sqrt(mind)

    dxl = cx[ind] - state.x
    dyl = cy[ind] - state.y

    angle = pi_2_pi(cyaw[ind] - math.atan2(dyl, dxl))
    if angle < 0:
        mind *= -1

    return ind, mind

def predict_motion(x0, oa, od, xref):
    xbar = xref * 0.0
    for i, _ in enumerate(x0):
        xbar[i, 0] = x0[i]

    state = State(x=x0[0], y=x0[1], yaw=x0[3], v=x0[2])
    for (ai, di, i) in zip(oa, od, range(1, T + 1)):
        state = update_state(state, ai, di)
        xbar[0, i] = state.x
        xbar[1, i] = state.y
        xbar[2, i] = state.v
        xbar[3, i] = state.yaw

    return xbar

def iterative_linear_mpc_control(xref, x0, dref, oa, od):
    """
    MPC control with updating operational point iteratively
    """
    ox, oy, oyaw, ov = None, None, None, None

    if oa is None or od is None:
        oa = [0.0] * T
        od = [0.0] * T

    for i in range(MAX_ITER):
        xbar = predict_motion(x0, oa, od, xref)
        poa, pod = oa[:], od[:]
        oa, od, ox, oy, oyaw, ov = linear_mpc_control(xref, xbar, x0, dref)
        du = sum(abs(oa - poa)) + sum(abs(od - pod))  # calc u change value
        if du <= DU_TH:
            break
    else:
        print("Iterative is max iter")

    return oa, od, ox, oy, oyaw, ov

def linear_mpc_control(xref, xbar, x0, dref):
    """
    linear mpc control

    xref: reference point
    xbar: operational point
    x0: initial state
    dref: reference steer angle
    """

    x = cvxpy.Variable((NX, T + 1))
    u = cvxpy.Variable((NU, T))

    cost = 0.0
    constraints = []

    for t in range(T):
        cost += cvxpy.quad_form(u[:, t], R)

        if t != 0:
            cost += cvxpy.quad_form(xref[:, t] - x[:, t], Q)

        A, B, C = get_linear_model_matrix(
            xbar[2, t], xbar[3, t], dref[0, t])
        constraints += [x[:, t + 1] == A @ x[:, t] + B @ u[:, t] + C]

        if t < (T - 1):
            cost += cvxpy.quad_form(u[:, t + 1] - u[:, t], Rd)
            constraints += [cvxpy.abs(u[1, t + 1] - u[1, t]) <=
                            MAX_DSTEER * DT]

    cost += cvxpy.quad_form(xref[:, T] - x[:, T], Qf)

    constraints += [x[:, 0] == x0]
    constraints += [x[2, :] <= MAX_SPEED]
    constraints += [x[2, :] >= MIN_SPEED]
    constraints += [cvxpy.abs(u[0, :]) <= MAX_ACCEL]
    constraints += [cvxpy.abs(u[1, :]) <= MAX_STEER]

    prob = cvxpy.Problem(cvxpy.Minimize(cost), constraints)
    prob.solve(solver=cvxpy.CLARABEL, verbose=False)

    if prob.status == cvxpy.OPTIMAL or prob.status == cvxpy.OPTIMAL_INACCURATE:
        ox = get_nparray_from_matrix(x.value[0, :])
        oy = get_nparray_from_matrix(x.value[1, :])
        ov = get_nparray_from_matrix(x.value[2, :])
        oyaw = get_nparray_from_matrix(x.value[3, :])
        oa = get_nparray_from_matrix(u.value[0, :])
        odelta = get_nparray_from_matrix(u.value[1, :])

    else:
        print("Error: Cannot solve mpc..")
        oa, odelta, ox, oy, oyaw, ov = None, None, None, None, None, None

    return oa, odelta, ox, oy, oyaw, ov

def calc_ref_trajectory(state, cx, cy, cyaw, ck, sp, dl, pind):
    xref = np.zeros((NX, T + 1))
    dref = np.zeros((1, T + 1))
    ncourse = len(cx)

    ind, _ = calc_nearest_index(state, cx, cy, cyaw, pind)

    if pind >= ind:
        ind = pind

    xref[0, 0] = cx[ind]
    xref[1, 0] = cy[ind]
    xref[2, 0] = sp[ind]
    xref[3, 0] = cyaw[ind]
    dref[0, 0] = 0.0  # steer operational point should be 0

    travel = 0.0

    for i in range(T + 1):
        travel += abs(state.v) * DT
        dind = int(round(travel / dl))

        if (ind + dind) < ncourse:
            xref[0, i] = cx[ind + dind]
            xref[1, i] = cy[ind + dind]
            xref[2, i] = sp[ind + dind]
            xref[3, i] = cyaw[ind + dind]
            dref[0, i] = 0.0
        else:
            xref[0, i] = cx[ncourse - 1]
            xref[1, i] = cy[ncourse - 1]
            xref[2, i] = sp[ncourse - 1]
            xref[3, i] = cyaw[ncourse - 1]
            dref[0, i] = 0.0

    return xref, ind, dref

def check_goal(state, goal, tind, nind):

    # check goal
    dx = state.x - goal[0]
    dy = state.y - goal[1]
    d = math.hypot(dx, dy)

    isgoal = (d <= GOAL_DIS or any(x < 0 for x in [dx,dy]))

    if abs(tind - nind) >= 5:
        isgoal = False

    isstop = (abs(state.v) <= STOP_SPEED)

    if isgoal and isstop:
        return True

    return False

def calc_speed_profile(cx, cy, cyaw, target_speed):

    speed_profile = [target_speed] * len(cx)
    direction = 1.0  # forward

    # Set stop point
    for i in range(len(cx) - 1):
        dx = cx[i + 1] - cx[i]
        dy = cy[i + 1] - cy[i]

        move_direction = math.atan2(dy, dx)

        if dx != 0.0 and dy != 0.0:
            dangle = abs(pi_2_pi(move_direction - cyaw[i]))
            if dangle >= math.pi / 4.0:
                direction = -1.0
            else:
                direction = 1.0

        if direction != 1.0:
            speed_profile[i] = - target_speed
        else:
            speed_profile[i] = target_speed

    speed_profile[-1] = 0.0

    return speed_profile

def smooth_yaw(yaw):

    for i in range(len(yaw) - 1):
        dyaw = yaw[i + 1] - yaw[i]

        while dyaw >= math.pi / 2.0:
            yaw[i + 1] -= math.pi * 2.0
            dyaw = yaw[i + 1] - yaw[i]

        while dyaw <= -math.pi / 2.0:
            yaw[i + 1] += math.pi * 2.0
            dyaw = yaw[i + 1] - yaw[i]

    return yaw

def load_arrays_from_file():
    arrays = []
    filename='/workspace/src/control/src/ws_mpc/odm_x_y_yaw_abs_log_7_straight.txt'
    with open(filename, 'r') as f:
        for line in f:
            x, y = map(float, line.strip().split(','))
            arrays.append(np.array([x, y]))
    return arrays

def reformatting(path):
    path_array = np.array(path) 
    ax = path_array[:,0].tolist()
    ay = path_array[:,1].tolist()
    return ax, ay 

def get_path(waypoints, dl=1):
    ax, ay = reformatting(waypoints)
    cx, cy, cyaw, ck, s = calc_spline_course(
        ax, ay, ds=dl)

    return cx, cy, cyaw, ck

def angle_norm(yaw_angle):
    """
    yaw_angle range is -1 ~ 1 
    """
    yaw_norm = np.abs(yaw_angle) / MAX_STEER
    yaw_norm = yaw_norm if yaw_angle >= 0 else -yaw_norm

    return yaw_norm

def accelerate_norm(acc_current):
    acc_norm = np.abs(acc_current) / MAX_ACCEL
    acc_norm = acc_norm if acc_current >= 0 else -acc_norm
    return acc_norm

def plot_array_data(data):
    # Convert the list of arrays to a numpy array
    points = np.array(data)
    
    # Extract x and y coordinates
    x = points[:, 0]
    y = points[:, 1]
    
    # Create a new figure
    plt.figure(figsize=(10, 6))
    
    # Plot the points
    plt.scatter(x, y, color='blue', s=50)
    
    # Plot lines connecting the points
    plt.plot(x, y, color='red', linestyle='--')
    
    # Set labels and title
    plt.xlabel('X-axis')
    plt.ylabel('Y-axis')
    plt.title('Plot of Array Data')
    
    # Add grid lines
    plt.grid(True, linestyle=':')
    
    # Show the plot
    plt.show()

def set_mpc_option():
    getinfo = GetControlInputInfo()
    infos = getinfo.run()
    lat, lon, yaw, speed, throttle, brake, steering = infos

    dl = 1.0  # course tick
    waypoints = load_arrays_from_file()

    ### local path planning -> spline generation
    cx, cy, cyaw, ck = get_path(waypoints, dl)
    ### speed profile for mpc
    sp = calc_speed_profile(cx, cy, cyaw, TARGET_SPEED)
    initial_state = State(x=lat, y=lon, yaw=yaw, v=speed)
    options = [cx, cy, cyaw, ck, sp, dl, initial_state, lat, lon, yaw, speed]
    return options, getinfo

class Control2Carla:
    def __init__(self, platform) -> None:
        vehicle = VehicleConfig(platform)
        self.ego_vehicle_controller = Ctrl_CV_CarlaEgoVehicelControl(vehicle.vehicle_config['Ego_Control'])

        # Create PID controllers for throttle, brake, and steering
        # Only P control is used (Ki and Kd are set to 0)
        self.throttle_pid = PID(Kp=10, Ki=0, Kd=0, setpoint=0, output_limits=(0, 1))
        self.brake_pid = PID(Kp=10, Ki=0, Kd=0, setpoint=0, output_limits=(0, 1))
        self.steering_pid = PID(Kp=10, Ki=0, Kd=0, setpoint=0, output_limits=(-1, 1))

    # Function to update control outputs
    def update_controls(self, throttle_input, brake_input, steering_input):
        # Calculate control outputs
        throttle_output = self.throttle_pid(throttle_input)
        brake_output = self.brake_pid(brake_input)
        steering_output = self.steering_pid(steering_input)
        
        return round(throttle_output,2), round(brake_output,2), round(steering_output,2),

    # def pid_scratch(self,):

class MPC_Controller():
    def __init__(self, initial_state, isPlot = False) -> None:
        self.isRuningMPC = False
        self.isPlot = isPlot        
        self.state = initial_state

        self.control2carla = Control2Carla('carla')

        self.control_data = {
            'throttle': 0.0,
            'brake': 0.0,
            'steer': 0.0,
            'hand_brake': False,
            'reverse': False,
            'gear': 0,
            'manual_gear_shift': False
        }

    def pub_msg(self, control_dict):
        self.control2carla.ego_vehicle_controller.pub_control_msg(control_dict)

    def update_pid_setpoint(self, throttle, brake, steering_norm):
        self.control2carla.throttle_pid.setpoint = throttle
        self.control2carla.brake_pid.setpoint = brake
        self.control2carla.steering_pid.setpoint = steering_norm

    def update_control_dict(self, di, ai, throttle_current, brake_current, steering_current):

        steering_norm = angle_norm(di)
        acc_norm = accelerate_norm(ai)

        if acc_norm >= 0:
            throttle = acc_norm
            brake = 0
        else:
            throttle = 0
            brake = -acc_norm
        
        self.update_pid_setpoint(throttle, brake, steering_norm)
        throttle_p, brake_p, steering_p = self.control2carla.update_controls(throttle_current, brake_current, steering_current)
        # throttle_p, brake_p, steering_p = throttle, brake, steering_norm

        control_data = {
            'throttle': throttle_p,
            'brake': brake_p,
            'steer': steering_p,
            'hand_brake': False,
            'reverse': False,
            'gear': 0,
            'manual_gear_shift': False
        }
        return control_data

    def do_mpc(self, options, getinfo):
        """
        Simulation
        Modified: Li Xingyou
        cx: course x position list (any)
        cy: course y position list (any)
        cyaw: course yaw position list (radian)
        ck: course curvature list
        sp: speed profile
        dl: course tick [m]

        """
        cx, cy, cyaw, ck, sp, dl, _, lat, lon, yaw, speed = options

        # initial yaw compensation
        if self.state.yaw - cyaw[0] >= math.pi:
            self.state.yaw -= math.pi * 2.0
        elif self.state.yaw - cyaw[0] <= -math.pi:
            self.state.yaw += math.pi * 2.0
        cyaw = smooth_yaw(cyaw)

        time = 0.0
        target_ind, _ = calc_nearest_index(self.state, cx, cy, cyaw, 0)
        odelta, oa = None, None
        goal = [cx[-1], cy[-1]]
        self.isRuningMPC = True
        while self.isRuningMPC:
            infos = getinfo.run()
            lat, lon, yaw, speed, throttle, brake, steering = infos

            xref, target_ind, dref = calc_ref_trajectory(
                self.state, cx, cy, cyaw, ck, sp, dl, target_ind)

            x0 = [self.state.x, self.state.y, self.state.v, self.state.yaw]  # current state

            oa, odelta, ox, oy, oyaw, ov = iterative_linear_mpc_control(
                xref, x0, dref, oa, odelta)

            di, ai = 0.0, 0.0
            if odelta is not None:
                di, ai = odelta[0], oa[0] # delta is steering, a is accelerate

                self.state = update_state_carla(self.state, lat, lon, yaw, speed)
            else:
                di, ai = None, None

            time += DT

            control_data = self.update_control_dict(di, ai, throttle, brake, steering)
            self.pub_msg(control_data)

            # Break the loop if goal is reached
            if check_goal(self.state, goal, target_ind, len(cx)) or MAX_TIME < time:
                print("Goal reached")
                break

            # Add a small delay to control the loop rate
        self.isRuningMPC = False

    def do_simulation(self, options):
        """
        Simulation

        cx: course x position list
        cy: course y position list
        cy: course yaw position list
        ck: course curvature list
        sp: speed profile
        dl: course tick [m]

        """
        cx, cy, cyaw, ck, sp, dl, _, _, _, _, _ = options

        goal = [cx[-1], cy[-1]]

        state = self.state

        # initial yaw compensation
        if state.yaw - cyaw[0] >= math.pi:
            state.yaw -= math.pi * 2.0
        elif state.yaw - cyaw[0] <= -math.pi:
            state.yaw += math.pi * 2.0

        time = 0.0
        x = [state.x]
        y = [state.y]
        yaw = [state.yaw]
        v = [state.v]
        t = [0.0]
        d = [0.0]
        a = [0.0]
        target_ind, _ = calc_nearest_index(state, cx, cy, cyaw, 0)

        odelta, oa = None, None

        cyaw = smooth_yaw(cyaw)

        while MAX_TIME >= time:
            xref, target_ind, dref = calc_ref_trajectory(
                state, cx, cy, cyaw, ck, sp, dl, target_ind)

            x0 = [state.x, state.y, state.v, state.yaw]  # current state

            oa, odelta, ox, oy, oyaw, ov = iterative_linear_mpc_control(
                xref, x0, dref, oa, odelta)

            di, ai = 0.0, 0.0
            if odelta is not None:
                di, ai = odelta[0], oa[0]
                state = update_state(state, ai, di)

            time = time + DT

            x.append(state.x)
            y.append(state.y)
            yaw.append(state.yaw)
            v.append(state.v)
            t.append(time)
            d.append(di)
            a.append(ai)

            if check_goal(state, goal, target_ind, len(cx)):
                print("Goal")
                break

            if show_animation:  # pragma: no cover
                plt.cla()
                # for stopping simulation with the esc key.
                plt.gcf().canvas.mpl_connect('key_release_event',
                        lambda event: [exit(0) if event.key == 'escape' else None])
                if ox is not None:
                    plt.plot(ox, oy, "xr", label="MPC")
                plt.plot(cx, cy, "-r", label="course")
                plt.plot(x, y, "ob", label="trajectory")
                plt.plot(xref[0, :], xref[1, :], "xk", label="xref")
                plt.plot(cx[target_ind], cy[target_ind], "xg", label="target")
                plot_car(state.x, state.y, state.yaw, steer=di)
                plt.axis("equal")
                plt.grid(True)
                plt.title("Time[s]:" + str(round(time, 2))
                        + ", speed[km/h]:" + str(round(state.v * 3.6, 2))
                        + ", acc[m/s^2]:" + str(round(ai, 2))
                        + ", steering[rad]:" + str(round(di, 2)))
                plt.pause(0.0001)

        return t, x, y, yaw, v, d, a

class GetControlInputInfo:
    def __init__(self) -> None:
        platform = 'carla'
        sensors = SensorConfig(platform)

        vehicle = VehicleConfig(platform)
        self.ego_vehicle_listner = CarlaEgoVehicle_Listener(vehicle.vehicle_config['Ego_Status'])

        self.odem_listener = Odem_Listener(sensors.sensor_config['Odem'])

        self.isODEMGet = False
        self.isVEHGet = False
        self.isYAWGet = False

    def update(self, infos):
        self.infos= infos

    def get_info(self,):
        return self.infos

    def run(self,):
        while not rospy.is_shutdown():
            self.ego_vehicle_listner.gathering_msg()
            self.odem_listener.gathering_msg()

            if self.odem_listener.data_received:
                ## odem with location and orientation
                last_location_pose = self.odem_listener.datas[-1].pose.pose
                last_location_position = last_location_pose.position
                lat, lon = last_location_position.x, last_location_position.y
                last_location_orientation = last_location_pose.orientation
                _, _, yaw = quat2euler(
                    [last_location_orientation.w,
                    last_location_orientation.x,
                    last_location_orientation.y,
                    last_location_orientation.z])
                yaw_degree = yaw # radian
                # yaw_degree = math.degrees(yaw)

                self.isODEMGet = True

            ## Get vehicle info: Speed, Orientation-> yaw
            if self.ego_vehicle_listner.data_received:
                data = self.ego_vehicle_listner.datas[-1]
                speed = data.velocity
                throttle = data.control.throttle
                brake = data.control.brake
                steering = data.control.steer
                self.isVEHGet = True

            if self.isODEMGet and self.isVEHGet:
                print(f'here is {__file__}')
                infos = [lat, lon, yaw_degree, speed, throttle, brake, steering] # if needs modify here to add or delete infos
                self.update(infos)
                return self.get_info()

def test_example_run(getinfo):
    ## Get lat, lon, yaw, speed
    return getinfo.run()
    
def test_example_run_mpc(show_animation = False):
    rospy.init_node('Test_mpc_controller')
    options, _ = set_mpc_option()
    initial_state = options[6]

    mpc_controller = MPC_Controller(initial_state)
    t, x, y, yaw, v, d, a = mpc_controller.do_simulation(options)

    if show_animation:  # pragma: no cover
        plt.close("all")
        plt.subplots()
        plt.plot(cx, cy, "-r", label="spline")
        plt.plot(x, y, "-g", label="tracking")
        plt.grid(True)
        plt.axis("equal")
        plt.xlabel("x[m]")
        plt.ylabel("y[m]")
        plt.legend()

        plt.subplots()
        plt.plot(t, v, "-r", label="speed")
        plt.grid(True)
        plt.xlabel("Time [s]")
        plt.ylabel("Speed [kmh]")

        plt.show()

def test_example_pub_with_mpc():
    rospy.init_node('Test_mpc_pub')
    options, getinfo = set_mpc_option()
    initial_state = options[6]


    mpc_controller = MPC_Controller(initial_state)
    mpc_controller.do_mpc(options, getinfo)

if __name__ == '__main__':
    unit_tests = ['getinfo', 'readwaypoints', 'mpc_controller', 'control2carla', ]
    unit_test = unit_tests[3]

    if unit_test == 'getinfo':
        rospy.init_node('Test_getinfo')
        getinfo = GetControlInputInfo()
        # getinfo.run()
        lat, lon, yaw, speed, throttle, brake, steering = test_example_run(getinfo)
        print()

    if unit_test == 'readwaypoints':
        waypoints = load_arrays_from_file()
        plot_array_data(waypoints)
        print()

    if unit_test == 'mpc_controller':
        show_animation = False
        test_example_run_mpc(show_animation)

    if unit_test == 'control2carla':
        test_example_pub_with_mpc()
        rospy.sleep(10)

