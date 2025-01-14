from __future__ import print_function

import torch
import torch.nn as nn
import torch.nn.functional as F
import math
import torchvision

from torch.optim import lr_scheduler
from torch.autograd import Variable


def squash(x):
    lengths2 = x.pow(2).sum(dim=2)
    lengths = lengths2.sqrt()
    x = x * (lengths2 / (1 + lengths2) / lengths).view(x.size(0), x.size(1), 1)
    return x


class AgreementRouting(nn.Module):
    def __init__(self, input_caps, output_caps, n_iterations):
        # input_caps 1152(32*6*6)
        # output_caps 10
        # n_iterations 3
        super(AgreementRouting, self).__init__()
        self.n_iterations = n_iterations
        self.b = nn.Parameter(torch.zeros((input_caps, output_caps)))

    def forward(self, u_predict):
        batch_size, input_caps, output_caps, output_dim = u_predict.size()
        # bs * 1152 * 10 * 16
        # b:1152,10
        c = F.softmax(self.b, dim=1)  # 1152 * 10
        s = (c.unsqueeze(2) * u_predict).sum(dim=1)  # bs * 10 * 16

        v = squash(s)

        # if n iteration = 0, means average the feature
        if self.n_iterations > 0:
            b_batch = self.b.expand((batch_size, input_caps, output_caps))
            # bs * 1152 * 10, 在每个capsule(10)上有一组权值(1152)，针对16维向量的加权
            for r in range(self.n_iterations):
                v = v.unsqueeze(1)  # bs * 1 * 10 * 16
                # u_predict:bs * 1152 * 10 * 16
                b_batch = b_batch + (u_predict * v).sum(-1)  # bs * 1152 * 10
                '''这里在整个batch的input_caps上做softmax，
                等于是将所有的1152*10个向量的权值除以了一个常数，
                即后续得到的s中的每个16维的最终输出向量，都是乘了一个常数，
                在最后的squash中，等价于分母的1除以了一个常数的平方
                '''
                # c = F.softmax(b_batch.view(-1, output_caps), dim=1).view(-1, input_caps, output_caps, 1)
                # 改成下面这样好像并无影响
                c = F.softmax(b_batch, dim=2).view(-1, input_caps, output_caps, 1)

                # bs * 1152 * 10 * 1
                s = (c * u_predict).sum(dim=1)
                # bs * 10 * 16
                v = squash(s)

        return v


class CapsLayer(nn.Module):
    def __init__(self, input_caps, input_dim, output_caps, output_dim, routing_module):
        # input_caps 32 * 6 * 6
        # input_dim 8
        # output_caps 10
        # output_dim 16
        super(CapsLayer, self).__init__()
        self.input_dim = input_dim
        self.input_caps = input_caps
        self.output_dim = output_dim
        self.output_caps = output_caps
        self.weights = nn.Parameter(torch.Tensor(input_caps, input_dim, output_caps * output_dim))
        self.routing_module = routing_module
        self.reset_parameters()

    def reset_parameters(self):
        stdv = 1. / math.sqrt(self.input_caps)
        self.weights.data.uniform_(-stdv, stdv)

    def forward(self, caps_output):
        # caps_output bs * 1152 * 8
        caps_output = caps_output.unsqueeze(2)  # bs * 1152 * 1 * 8
        # self.weights 1152(32*6*6) * 8 * 160
        u_predict = caps_output.matmul(self.weights)  # bs * 1152 * 1 * 160
        u_predict = u_predict.view(u_predict.size(0), self.input_caps, self.output_caps, self.output_dim)
        # bs * 1152(32*6*6) * 10 * 16
        v = self.routing_module(u_predict)
        return v


class PrimaryCapsLayer(nn.Module):
    def __init__(self, input_channels, output_caps, output_dim, kernel_size, stride):
        # output_caps 32
        # output_dim 8
        # kernel size 9
        # stride 2
        super(PrimaryCapsLayer, self).__init__()
        self.conv = nn.Conv2d(input_channels, output_caps * output_dim, kernel_size=kernel_size, stride=stride)
        self.input_channels = input_channels
        self.output_caps = output_caps
        self.output_dim = output_dim

    def forward(self, input):
        out = self.conv(input)
        N, C, H, W = out.size()
        # bs * 256(32*8) * 6 * 6
        out = out.view(N, self.output_caps, self.output_dim, H, W)
        # will output N x OUT_CAPS x OUT_DIM
        # OUT_DIM = output channels
        # OUT_CAPS = num of convs
        out = out.permute(0, 1, 3, 4, 2).contiguous()
        # bs * OUT_CAPS * H * W * output_dim  bs * 32 * 6 * 6 * 8
        out = out.view(out.size(0), -1, out.size(4))
        # bs * 1152(32*6*6) * 8
        out = squash(out)
        return out


class CapsNet(nn.Module):
    def __init__(self, routing_iterations, in_channels=1, n_classes=10):
        super(CapsNet, self).__init__()
        self.conv1 = nn.Conv2d(in_channels, 256, kernel_size=9, stride=1)
        self.primaryCaps = PrimaryCapsLayer(256, 32, 8, kernel_size=9, stride=2)  # outputs 6*6
        self.num_primaryCaps = 32 * 6 * 6 if in_channels == 1 else 32 * 8 * 8
        routing_module = AgreementRouting(self.num_primaryCaps, n_classes, routing_iterations)
        self.digitCaps = CapsLayer(self.num_primaryCaps, 8, n_classes, 16, routing_module)

    def forward(self, input):
        x = self.conv1(input)
        x = F.relu(x)  # bs * 256 * 20 * 20
        x = self.primaryCaps(x)  # bs * 1152(32*6*6) * 8
        x = self.digitCaps(x)  # bs * 10 * 16
        probs = x.pow(2).sum(dim=2).sqrt()  # bs * 10
        return x, probs


class ReconstructionNet(nn.Module):
    def __init__(self, n_dim=16, n_classes=10):
        super(ReconstructionNet, self).__init__()
        self.fc1 = nn.Linear(n_dim * n_classes, 512)
        self.fc2 = nn.Linear(512, 1024)
        self.fc3 = nn.Linear(1024, 784)
        self.n_dim = n_dim
        self.n_classes = n_classes

    def forward(self, x, target):
        mask = Variable(torch.zeros((x.size()[0], self.n_classes)), requires_grad=False)
        # bs * 10
        if next(self.parameters()).is_cuda:
            mask = mask.cuda()
        # target 128
        mask.scatter_(1, target.view(-1, 1), 1.)
        mask = mask.unsqueeze(2)  # bs * 10 * 1
        #  x: bs * 10 * 16
        x = x * mask  # bs * 10 * 16
        x = x.view(-1, self.n_dim * self.n_classes)  # bs * 160
        x = F.relu(self.fc1(x))
        x = F.relu(self.fc2(x))
        x = F.sigmoid(self.fc3(x))
        return x


class CapsNetWithReconstruction(nn.Module):
    def __init__(self, capsnet, reconstruction_net):
        super(CapsNetWithReconstruction, self).__init__()
        self.capsnet = capsnet
        self.reconstruction_net = reconstruction_net

    def forward(self, x, target):
        x, probs = self.capsnet(x)
        reconstruction = self.reconstruction_net(x, target)
        return reconstruction, probs


class MarginLoss(nn.Module):
    def __init__(self, m_pos, m_neg, lambda_):
        super(MarginLoss, self).__init__()
        self.m_pos = m_pos
        self.m_neg = m_neg
        self.lambda_ = lambda_

    def forward(self, lengths, targets, size_average=True):
        t = torch.zeros(lengths.size()).long()
        if targets.is_cuda:
            t = t.cuda()
        t = t.scatter_(1, targets.data.view(-1, 1), 1)
        targets = Variable(t)
        losses = targets.float() * F.relu(self.m_pos - lengths).pow(2) + \
                 self.lambda_ * (1. - targets.float()) * F.relu(lengths - self.m_neg).pow(2)
        return losses.mean() if size_average else losses.sum()


if __name__ == '__main__':

    import argparse
    import torch.optim as optim
    from torchvision import datasets, transforms
    from torch.autograd import Variable

    # Training settings
    parser = argparse.ArgumentParser(description='CapsNet with MNIST')
    parser.add_argument('--batch-size', type=int, default=128, metavar='N',
                        help='input batch size for training (default: 64)')
    parser.add_argument('--test-batch-size', type=int, default=1000, metavar='N',
                        help='input batch size for testing (default: 1000)')
    parser.add_argument('--epochs', type=int, default=250, metavar='N',
                        help='number of epochs to train (default: 10)')
    parser.add_argument('--lr', type=float, default=0.001, metavar='LR',
                        help='learning rate (default: 0.01)')
    parser.add_argument('--no-cuda', action='store_true', default=False,
                        help='disables CUDA training')
    parser.add_argument('--seed', type=int, default=1, metavar='S',
                        help='random seed (default: 1)')
    parser.add_argument('--log-interval', type=int, default=10, metavar='N',
                        help='how many batches to wait before logging training status')
    parser.add_argument('--routing_iterations', type=int, default=3)
    parser.add_argument('--with_reconstruction', action='store_true', default=False)
    parser.add_argument('--dataset', type=str, default='mnist')
    args = parser.parse_args()
    args.cuda = not args.no_cuda and torch.cuda.is_available()

    torch.manual_seed(args.seed)
    if args.cuda:
        torch.cuda.manual_seed(args.seed)

    kwargs = {'num_workers': 1, 'pin_memory': True} if args.cuda else {}

    if args.dataset == 'mnist':
        train_loader = torch.utils.data.DataLoader(
            datasets.MNIST('../data', train=True, download=True,
                           transform=transforms.Compose([
                               transforms.Pad(2), transforms.RandomCrop(28),
                               transforms.ToTensor()
                           ])),
            batch_size=args.batch_size, shuffle=True, **kwargs)

        test_loader = torch.utils.data.DataLoader(
            datasets.MNIST('../data', train=False, transform=transforms.Compose([
                transforms.ToTensor()
            ])),
            batch_size=args.test_batch_size, shuffle=False, **kwargs)
        in_channels = 1
    elif args.dataset == 'cifar10':
        train_data = torchvision.datasets.CIFAR10(root='../resnet/data/',
                                                  train=True,
                                                  transform=transforms.Compose([
                                                      transforms.RandomCrop(32, 4),
                                                      transforms.RandomHorizontalFlip(),
                                                      transforms.ToTensor(),
                                                      transforms.Normalize((0.4914, 0.4822, 0.4465),
                                                                           (0.2023, 0.1994, 0.2010)),
                                                  ]),
                                                  download=True)
        test_data = torchvision.datasets.CIFAR10(root='../resnet/data/',
                                                 train=False,
                                                 transform=transforms.Compose([
                                                     transforms.ToTensor(),
                                                     transforms.Normalize((0.4914, 0.4822, 0.4465),
                                                                          (0.2023, 0.1994, 0.2010))
                                                 ]),
                                                 download=True)
        train_loader = torch.utils.data.DataLoader(train_data, batch_size=args.batch_size, shuffle=True, **kwargs)
        test_loader = torch.utils.data.DataLoader(test_data, batch_size=args.batch_size, shuffle=False, **kwargs)
        in_channels = 3
    else:
        raise NotImplementedError

    model = CapsNet(args.routing_iterations, in_channels)

    if args.with_reconstruction:
        reconstruction_model = ReconstructionNet(16, 10)
        reconstruction_alpha = 0.0005
        model = CapsNetWithReconstruction(model, reconstruction_model)

    if args.cuda:
        model.cuda()

    optimizer = optim.Adam(model.parameters(), lr=args.lr)

    scheduler = lr_scheduler.ReduceLROnPlateau(optimizer, verbose=True, patience=15, min_lr=1e-6)

    loss_fn = MarginLoss(0.9, 0.1, 0.5)


    def train(epoch):
        model.train()
        for batch_idx, (data, target) in enumerate(train_loader):
            if args.cuda:
                data, target = data.cuda(), target.cuda()
            data, target = Variable(data), Variable(target, requires_grad=False)
            optimizer.zero_grad()
            if args.with_reconstruction:
                output, probs = model(data, target)
                reconstruction_loss = F.mse_loss(output, data.view(-1, 784))
                margin_loss = loss_fn(probs, target)
                loss = reconstruction_alpha * reconstruction_loss + margin_loss
            else:
                output, probs = model(data)
                loss = loss_fn(probs, target)
            loss.backward()
            optimizer.step()
            if batch_idx % args.log_interval == 0:
                print('Train Epoch: {} [{}/{} ({:.0f}%)]\tLoss: {:.6f}'.format(
                    epoch, batch_idx * len(data), len(train_loader.dataset),
                           100. * batch_idx / len(train_loader), loss.item()))


    def test():
        model.eval()
        test_loss = 0
        correct = 0
        for data, target in test_loader:
            if args.cuda:
                data, target = data.cuda(), target.cuda()
            data, target = Variable(data, volatile=True), Variable(target)

            if args.with_reconstruction:
                output, probs = model(data, target)
                reconstruction_loss = F.mse_loss(output, data.view(-1, 784), size_average=False).item()
                test_loss += loss_fn(probs, target, size_average=False).item()
                test_loss += reconstruction_alpha * reconstruction_loss
            else:
                output, probs = model(data)
                test_loss += loss_fn(probs, target, size_average=False).item()

            pred = probs.data.max(1, keepdim=True)[1]  # get the index of the max probability
            correct += pred.eq(target.data.view_as(pred)).cpu().sum()

        test_loss /= len(test_loader.dataset)
        print('\nTest set: Average loss: {:.4f}, Accuracy: {}/{} ({:.0f}%)\n'.format(
            test_loss, correct, len(test_loader.dataset),
            100. * correct / len(test_loader.dataset)))
        return test_loss


    for epoch in range(1, args.epochs + 1):
        train(epoch)
        test_loss = test()
        scheduler.step(test_loss)
        torch.save(model.state_dict(),
                   '{:03d}_model_dict_{}routing_reconstruction{}.pth'.format(epoch, args.routing_iterations,
                                                                             args.with_reconstruction))
