import math

import torch
import torch.nn as nn

from nms import AnchornizedNMS
from general import check_version

from data_table import DATASET_NUM_DIMS


class ConvModule(nn.Module):
    def __init__(self, c_in, c_out, k, s, p, act, groups=1):
        super().__init__()
        self.conv = nn.Conv2d(c_in, c_out, k, s, p, bias=False, groups=groups)
        self.bn = nn.BatchNorm2d(c_out)
        if act == "relu6":
            self.relu = nn.ReLU6()
        elif act == "relu":
            self.relu = nn.ReLU()
        elif act == "leakyrelu":
            self.relu = nn.LeakyReLU()
        elif act == "hardswish":
            self.relu = nn.Hardswish()
        else:
            raise NotImplementedError(f"conv with activation={act} not implemented yet")
        self.fused = False

    def forward(self, x):
        x = self.conv(x)
        x = self.bn(x)
        x = self.relu(x)
        return x

    def fused_forward(self, x):
        return self.relu(self.conv(x))

    def fuse(self):
        if self.fused:
            return self
        std = (self.bn.running_var + self.bn.eps).sqrt()
        bias = self.bn.bias - self.bn.running_mean * self.bn.weight / std

        t = (self.bn.weight / std).reshape(-1, 1, 1, 1)
        weights = self.conv.weight * t

        self.conv = nn.Conv2d(
            in_channels=self.conv.in_channels,
            out_channels=self.conv.out_channels,
            kernel_size=self.conv.kernel_size,
            stride=self.conv.stride,
            padding=self.conv.padding,
            dilation=self.conv.dilation,
            groups=self.conv.groups,
            bias=True,
            padding_mode=self.conv.padding_mode,
        )
        self.conv.weight = torch.nn.Parameter(weights)
        self.conv.bias = torch.nn.Parameter(bias)
        self.forward = self.fused_forward
        self.fused = True
        return self


class DarknetBottleneck(nn.Module):
    def __init__(self, c_in, c_out, add=True, act=None):
        super().__init__()
        self.cv1 = ConvModule(c_in, int(0.5 * c_in), 3, 1, 1, act=act)
        self.cv2 = ConvModule(int(0.5 * c_in), c_out, 3, 1, 1, act=act)
        self.shortcut = add

    def forward(self, x):
        if self.shortcut:
            out = self.cv1(x)
            out = self.cv2(out)
            return x + out
        else:
            x = self.cv1(x)
            x = self.cv2(x)
            return x


class CSPLayer_2Conv(nn.Module):
    def __init__(self, c_in, c_out, add, n, act):
        super().__init__()
        half_out = int(0.5 * c_out)
        self.conv_in_left = ConvModule(c_in, half_out, 1, 1, 0, act=act)  # same result as split later
        self.conv_in_right = ConvModule(c_in, half_out, 1, 1, 0, act=act)  # same result as split later
        self.bottlenecks = nn.ModuleList()
        for _ in range(n):
            self.bottlenecks.append(DarknetBottleneck(half_out, half_out, add, act=act))
        self.conv_out = ConvModule(half_out * (n + 2), c_out, 1, 1, 0, act=act)

    def forward(self, x):
        x_left = self.conv_in_left(x)
        x_right = self.conv_in_right(x)  # main branch
        collection = [x_left, x_right]
        x = x_right
        for b in self.bottlenecks:
            x = b(x)
            collection.append(x)
        x = torch.cat(collection, dim=1)
        x = self.conv_out(x)
        return x


class SPPF(nn.Module):
    # Spatial Pyramid Pooling - Fast (SPPF) layer for YOLOv5 by Glenn Jocher
    def __init__(self, c_in, k=5, act=None):  # equivalent to SPP(k=(5, 9, 13))
        super().__init__()
        c_ = c_in // 2  # hidden channels
        self.cv1 = ConvModule(c_in, c_, 1, 1, 0, act=act)
        self.cv2 = ConvModule(c_ * 4, c_in, 1, 1, 0, act=act)
        self.m = nn.MaxPool2d(kernel_size=k, stride=1, padding=k // 2)

    def forward(self, x):
        x = self.cv1(x)
        y1 = self.m(x)
        y2 = self.m(y1)
        y3 = self.m(y2)
        return self.cv2(torch.cat((x, y1, y2, y3), 1))


class YOLOv8Backbone(nn.Module):
    def __init__(self, d, w, r, act):
        super().__init__()
        _64xw = int(64 * w)
        _128xw = int(128 * w)
        _256xw = int(256 * w)
        _512xw = int(512 * w)
        _512xwxr = int(512 * w * r)
        _3xd = int(math.ceil(3 * d))
        _6xd = int(math.ceil(6 * d))
        self.stem_layer = ConvModule(3, _64xw, k=3, s=2, p=1, act=act)
        self.stage_layer_1 = nn.Sequential(
            ConvModule(_64xw, _128xw, k=3, s=2, p=1, act=act),
            CSPLayer_2Conv(_128xw, _128xw, add=True, n=_3xd, act=act),
        )
        self.stage_layer_2 = nn.Sequential(
            ConvModule(_128xw, _256xw, k=3, s=2, p=1, act=act),
            CSPLayer_2Conv(_256xw, _256xw, add=True, n=_6xd, act=act),
        )
        self.stage_layer_3 = nn.Sequential(
            ConvModule(_256xw, _512xw, k=3, s=2, p=1, act=act),
            CSPLayer_2Conv(_512xw, _512xw, add=True, n=_6xd, act=act),
        )
        self.stage_layer_4 = nn.Sequential(
            ConvModule(_512xw, _512xwxr, k=3, s=2, p=1, act=act),
            CSPLayer_2Conv(_512xwxr, _512xwxr, add=True, n=_3xd, act=act),
            SPPF(_512xwxr, act=act),
        )

    def forward(self, x):
        p1 = self.stem_layer(x)
        p2 = self.stage_layer_1(p1)
        p3 = self.stage_layer_2(p2)
        p4 = self.stage_layer_3(p3)
        p5 = self.stage_layer_4(p4)
        return p3, p4, p5


class YOLOv8Neck(nn.Module):
    def __init__(self, d, w, r, act) -> None:
        _3xd = int(math.ceil(3 * d))
        _256xw = int(256 * w)
        _512xw = int(512 * w)
        super().__init__()
        self.upsample_p5 = nn.Upsample(None, 2, "nearest")
        self.upsample_p4 = nn.Upsample(None, 2, "nearest")
        self.topdown_layer_2 = CSPLayer_2Conv(int(512 * w * (1 + r)), _256xw, add=False, n=_3xd, act=act)
        self.topdown_layer_1 = CSPLayer_2Conv(_512xw, _256xw, add=False, n=_3xd, act=act)
        self.down_sample_0 = ConvModule(_256xw, _256xw, k=3, s=2, p=1, act=act)
        self.bottomup_layer_0 = CSPLayer_2Conv(_512xw, _512xw, add=False, n=_3xd, act=act)
        self.down_sample_1 = ConvModule(_512xw, _512xw, k=3, s=2, p=1, act=act)
        self.bottomup_layer_1 = CSPLayer_2Conv(int(512 * w * (1 + r)), int(512 * w * r), add=False, n=_3xd, act=act)

    def forward(self, x):
        p3, p4, p5 = x
        # top-down P5->P4
        u1 = self.upsample_p5(p5)
        # forward in P4
        c1 = torch.cat((p4, u1), dim=1)
        t1 = self.topdown_layer_2(c1)
        # top-down P4->P3
        u2 = self.upsample_p4(t1)
        # forward in P3
        c2 = torch.cat((p3, u2), dim=1)
        t2 = self.topdown_layer_1(c2)
        # bottom-up P3->P4
        d1 = self.down_sample_0(t2)
        # forward in P4
        c3 = torch.cat((t1, d1), dim=1)
        b1 = self.bottomup_layer_0(c3)
        # bottom-up P4->P5
        d2 = self.down_sample_1(b1)
        # forward in P5
        c4 = torch.cat((p5, d2), dim=1)
        b2 = self.bottomup_layer_1(c4)
        return t2, b1, b2


class MergedHead(nn.Module):
    def __init__(self, w, out_channels, act):
        super().__init__()
        self.block = nn.Sequential(
            ConvModule(w, w, k=3, s=1, p=1, act=act),
            ConvModule(w, w, k=3, s=1, p=1, act=act),
            nn.Conv2d(w, out_channels, kernel_size=1, stride=1),
        )

    def forward(self, x):
        return self.block(x)


class AnchornizedFaceQuality(nn.Module):
    def __init__(self, w, r, act, strides=(8, 16, 32), anchors=None, anchor_t=4.0, conf_threshold=0.01, iou_threshold=0.6):
        super().__init__()
        if anchors is None:
            anchors = (
                (10, 13, 16, 30, 33, 23),
                (30, 61, 62, 45, 59, 119),
                (116, 90, 156, 198, 373, 326),
            )
        self.nattr = DATASET_NUM_DIMS - 1 - 4  # class_id, location
        self.nl = len(anchors)  # number of detection layers
        self.na = len(anchors[0]) // 2  # number of anchors
        self.grid = [torch.zeros(1)] * self.nl  # init grid
        self.anchor_grid = [torch.zeros(1)] * self.nl  # init anchor grid
        self.register_buffer("anchors", torch.tensor(anchors).float().view(self.nl, -1, 2))  # shape(nl,na,2)
        self.strides = torch.tensor(strides)  # strides computed during build
        self.anchor_t = anchor_t
        self.nms = AnchornizedNMS(1, conf_threshold, iou_threshold)
        self.porting = False

        out_channels = (4 + 1 + self.nattr) * self.na  # box, obj
        self.head_p3 = MergedHead(int(256 * w), out_channels, act)
        self.head_p4 = MergedHead(int(512 * w), out_channels, act)
        self.head_p5 = MergedHead(int(512 * w * r), out_channels, act)
        self.anchors_for_porting = []

    def bias_init(self, stride):
        for mi, stride in zip([self.head_p3.block[-1], self.head_p4.block[-1], self.head_p5.block[-1]], self.strides):  # from
            b = mi.bias.view(-1, 5 + self.nattr)  # conv.bias(255) to (3,85)
            b.data[:, 4] += math.log(8 / (640 / float(stride)) ** 2)  # obj (8 objects per 640 image)
            mi.bias = torch.nn.Parameter(b.view(-1), requires_grad=True)
        return self

    def _make_grid(self, nx=20, ny=20, i=0, torch_1_10=check_version(torch.__version__, "1.10.0")):
        d = self.anchors[i].device
        t = self.anchors[i].dtype
        shape = 1, self.na, ny, nx, 2  # grid shape
        y, x = torch.arange(ny, device=d, dtype=t), torch.arange(nx, device=d, dtype=t)
        yv, xv = torch.meshgrid(y, x, indexing="ij") if torch_1_10 else torch.meshgrid(y, x)  # torch>=0.7 compatibility
        grid = torch.stack((xv, yv), 2).expand(shape)  # add grid offset, i.e. y = 2.0 * x - 0.5
        anchor_grid = (self.anchors[i] * self.strides[i]).view((1, self.na, 1, 1, 2)).expand(shape)
        if self.porting:
            for x in (self.anchors[i] * self.strides[i]).flatten():
                self.anchors_for_porting.append(x.item())
        return grid, anchor_grid

    def _reshape(self, x):
        (N, C, H, W), A = x.shape, self.na
        return x.view(N, A, int(C / A), H, W).permute(0, 1, 3, 4, 2).contiguous()

    def forward(self, x):
        p3, p4, p5 = x
        x_p3 = self.head_p3(p3)
        x_p4 = self.head_p4(p4)
        x_p5 = self.head_p5(p5)
        x = [x_p3, x_p4, x_p5]

        if self.porting:
            outputs = []
            for i in range(self.nl):
                x[i] = x[i].sigmoid()
                N, C, H, W = x[i].shape  # print anchor grid
                self._make_grid(W, H, i)
                outputs.append(x[i].view(N, C, -1))
            # outputs = torch.cat(outputs, dim=-1)
            return outputs

        outputs = []
        for i in range(self.nl):
            x[i] = self._reshape(x[i])

            if not self.training:
                # make grid
                N, A, H, W, C = x[i].shape
                if self.grid[i].shape[2:4] != x[i].shape[2:4]:
                    self.grid[i], self.anchor_grid[i] = self._make_grid(W, H, i)
                # normalize and get xywh
                y = x[i].sigmoid()
                G, S, AS = self.grid[i], self.strides[i], self.anchor_grid[i]
                y[..., 0:2] = (y[..., 0:2] * 2.0 - 0.5 + G) * S  # (x + grid) * stride
                y[..., 2:4] = (y[..., 2:4] * 2.0) * (y[..., 2:4] * 2.0) * AS  # x * anchor * stride
                outputs.append(y.view(N, A * H * W, C))

        if self.training:
            return x

        outputs = torch.cat(outputs, dim=1)  # xywh + sigmoid(cls) for each grid
        outputs = self.nms(outputs)
        return outputs


def init_model_params(model: nn.Module):
    for m in model.modules():
        t = type(m)
        if t is nn.Conv2d:
            pass  # nn.init.kaiming_normal_(m.weight, mode='fan_out', nonlinearity='relu')
        elif t is nn.BatchNorm2d:
            m.eps = 1e-3
            m.momentum = 0.03
        elif t in [nn.Hardswish, nn.LeakyReLU, nn.ReLU, nn.ReLU6, nn.SiLU]:
            m.inplace = True
    return model


class Model(nn.Module):
    def __init__(self, d=0.33, w=0.25, r=2.0, act="relu6") -> None:
        super().__init__()
        self.backbone = YOLOv8Backbone(d, w, r, act)
        self.neck = YOLOv8Neck(d, w, r, act)
        self.head = AnchornizedFaceQuality(w, r, act)
        self.init_params()

    def init_params(self):
        for m in self.modules():
            t = type(m)
            if t is nn.Conv2d:
                pass  # nn.init.kaiming_normal_(m.weight, mode='fan_out', nonlinearity='relu')
            elif t is nn.BatchNorm2d:
                m.eps = 1e-3
                m.momentum = 0.03
            elif t in [nn.Hardswish, nn.LeakyReLU, nn.ReLU, nn.ReLU6, nn.SiLU]:
                m.inplace = True

    def forward(self, x):
        x = self.backbone(x)
        x = self.neck(x)
        x = self.head(x)
        return x


def yolov8n_facecap_v2():
    return Model(d=0.33, w=0.25, r=2.0, act="relu6")
