from ..devices import StaticMemristor, DynamicMemristor
import torch
import numpy as np


class LineResistanceCrossbar:
    """
    The crossbar model takes in voltage vector v and perform VMM with conductances W on the crossbar
        returns:  Wv
    This class does not:
        Normalize input. So all input should be normalized in the range for the memristor model
            e.g. for the static/dynamic memristor:
            3.16e-6 to 316e-6 S for conductance
            -0.4 to 0.4 V for inference
        Normalize output.
    """
    def __init__(self, memristor_model, memristor_params, ideal_w, crossbar_params):
        """
        :param memristor_model: memristor model class
        :param memristor_params: dictionary of the model param
        :param ideal_w: nxm numpy/torch matrix of ideal conductances be programed
        """
        self.memristor_model = memristor_model
        self.memristor_params = memristor_params
        self.memristors = [[initialize_memristor(memristor_model, memristor_params, ideal_w[i, j])
                            for j in range(ideal_w.shape[1])] for i in range(ideal_w.shape[0])]
        self.ideal_w = ideal_w
        self.n, self.m = ideal_w.shape
        self.fitted_w = torch.tensor([[self.memristors[i][j].g_linfit for j in range(ideal_w.shape[1])]
                                      for i in range(ideal_w.shape[0])]).squeeze()
        self.cache = {}  # cache useful statistics to avoid redundant calculations

        # conductance of the word and bit lines.
        self.g_wl = torch.Tensor((1 / crossbar_params["r_wl"],))
        self.g_bl = torch.Tensor((1 / crossbar_params["r_bl"],))

        # WL & BL resistances
        self.r_in = crossbar_params["r_in"]
        self.r_out = crossbar_params["r_out"]

        # line conductance of the sensor lines
        self.g_s_wl_in = torch.ones(self.m) / self.r_in
        self.g_s_wl_out = torch.ones(self.m) * 1e-15  # floating
        self.g_s_bl_in = torch.ones(self.n) * 1e-15  # floating
        self.g_s_bl_out = torch.ones(self.n) / self.r_out

        # WL & BL voltages that are not the signal, assume bl_in, wl_out are tied low and bl_out is tied to 1 V.
        self.v_bl_in = torch.zeros(self.n)
        self.v_bl_out = torch.zeros(self.n)
        self.v_wl_out = torch.zeros(self.m)

    def ideal_vmm(self, v):
        """
        idealized vmm
        dims:
            v: mx1
            ideal_w: nxm
        """
        return torch.matmul(self.ideal_w, v)

    def naive_linear_memristive_vmm(self, v):
        """
        idealized vmm using fitted conductance of the memristors
        dims:
            v: mx1
            fitted_w: nxm
        """
        return torch.matmul(self.fitted_w, v)

    def naive_memristive_vmm(self, v):
        """
        vmm with non-ideal memristor inference and ideal crossbar
        dims:
            v: mx1
            crossbar: nxm
        """
        def mac_op(a1, a2):
            return torch.sum(torch.tensor([a1[j].inference(a2[j]) for j in range(len(a1))]))
        ret = torch.zeros([self.ideal_w.shape[0]])
        for i, row in enumerate(self.memristors):
            ret[i] = mac_op(row, v)
        return ret

    def lineres_memristive_vmm(self, v_applied, iter=1):
        """
        vmm with non-ideal memristor inference and ideal crossbar
        dims:
            v_dd: mx1
            ideal_w: nxm
        :param v_applied: mx1 word line applied analog voltage
        :param iter: int. iter = 0 is constant conductance, iter = 1 is default first order g(v) approximation
                          iter = 2 is second order... and so on.
        :return: nx1 analog current vector
        """
        W = self.fitted_w
        V_crossbar = self.solve_v(W, v_applied)
        for i in range(iter):
            V_crossbar = V_crossbar.view([-1, self.m, self.n])  # 2xmxn
            V_wl, V_bl = torch.t(V_crossbar[0,:,:]), torch.t(V_crossbar[1,:,:])  # now nxm
            V_diff = V_bl - V_wl
            W = torch.tensor([[self.memristors[i][j].inference(V_diff[i,j]) for j in range(self.m)]
                              for i in range(self.n)])/V_diff
            V_crossbar = self.solve_v(W, v_applied)
        V_wl, V_bl = torch.t(V_crossbar[0, :, :]), torch.t(V_crossbar[1, :, :])  # now nxm
        V_diff = V_bl - V_wl
        I = V_diff*W # nxm
        return torch.sum(I, dim=1)

    def solve_v(self, W, v_applied):
        """
        m word lines and n bit lines
        let M = [A, B; C, D]
        solve MV=E
        :param W: nxm matrix of conductance, type torch tensor
        :param v_applied: mx1 word line applied analog voltage
        :return V: 2mn x 1 vector contains voltages of the word line and bit line
        """
        A = self.make_A(W)
        B = self.make_B(W)
        C = self.make_C(W)
        D = self.make_D(W)
        E = self.make_E(v_applied)
        M = torch.cat((torch.cat((A, B), 1), torch.cat((C, D), 1)), 0)
        M_inv = torch.inverse(M)
        self.cache["M_inv"] = M_inv
        return torch.matmul(M_inv, E)

    def make_E(self, v_applied):
        m, n = self.m, self.n
        E_B = torch.cat([torch.cat(((-self.v_bl_in[i] * self.g_s_bl_in[i]).view(1), torch.zeros(n-2), (-self.v_bl_in[i] * self.g_s_bl_out[i]).view(1))).unsqueeze(1) for i in range(m)])
        E_W = torch.cat([torch.cat(((v_applied[i] * self.g_s_wl_in[i]).view(1), torch.zeros(n-2), (self.v_wl_out[i].view(1) * self.g_s_wl_out[i]).view(1))) for i in range(m)]).unsqueeze(1)
        return torch.cat((E_W, E_B))

    def make_A(self, W):
        W_t = torch.t(W)
        m, n = self.m, self.n

        def makea(i):
            return torch.diag(W_t[i, :]) \
                   + torch.diag(torch.cat((self.g_wl, self.g_wl * 2 * torch.ones(n - 2), self.g_wl))) \
                   + torch.diag(self.g_wl * -1 * torch.ones(n - 1), diagonal=1) \
                   + torch.diag(self.g_wl * -1 * torch.ones(n - 1), diagonal=-1) \
                   + torch.diag(torch.cat((self.g_s_wl_in[i].view(1), torch.zeros(n - 2), self.g_s_wl_out[i].view(1))))

        return torch.block_diag(*tuple(makea(i) for i in range(m)))

    def make_B(self, W):
        W_t = torch.t(W)
        m, n = self.m, self.n
        return torch.block_diag(*tuple(-torch.diag(W_t[i,:]) for i in range(m)))

    def make_C(self, W):
        W_t = torch.t(W)
        m, n = self.m, self.n

        def makec(j):
            return torch.zeros(m, m*n).index_put((torch.arange(m), torch.arange(m) * n + j), W_t[:, j])

        torch.cat([makec(j) for j in range(n)],dim=0)

    def make_D(self, W):
        W_t = torch.t(W)
        m, n = self.m, self.n

        def maked(j):
            d = torch.zeros(m, m * n)

            i = 0
            d[i, j] = -self.g_s_bl_in[j] - self.g_bl - W_t[i, j]
            d[i, n * (i + 1) + j] = self.g_bl

            for i in range(1, m):
                d[i, n * (i - 1) + j] = self.g_bl
                d[i, n * i + j] = -self.g_bl - W_t[i, j] - self.g_bl
                d[i, j] = self.g_bl

            i = m - 1
            d[i, n * (i - 1) + j] = self.g_bl
            d[i, n * i + j] = -self.g_s_bl_out[j] - W_t[i, j] - self.g_bl

            return d

        return torch.cat([maked(j) for j in range(0,n)], dim=0)


def initialize_memristor(memristor_model, memristor_params, g_0):
    """
    :param memristor_model: model to use
    :param memristor_params: parameter
    :param g_0: initial conductance
    :return: an unique calibrated memristor
    """
    if memristor_model == StaticMemristor:
        memristor = StaticMemristor(g_0)
        memristor.calibrate(memristor_params["temperature"], memristor_params["frequency"])
        return memristor
    elif memristor_model == DynamicMemristor:
        memristor = DynamicMemristor(g_0)
        memristor.calibrate(memristor_params["temperature"], memristor_params["frequency"])
        return memristor
    else:
        raise Exception('Invalid memristor model')
