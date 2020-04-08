import torch
import itertools
from util.image_pool import ImagePool
from .base_model import BaseModel
from . import networks
import torch.nn.functional as F


class UnpairedRevGANModel(BaseModel):
    ''' Unpaired 2D-RevGAN model '''
    @staticmethod
    def modify_commandline_options(parser, is_train=True):
        if is_train:
            parser.add_argument('--lambda_A', type=float, default=10.0, help='weight for cycle loss (A -> B -> A)')
            parser.add_argument('--lambda_B', type=float, default=10.0, help='weight for cycle loss (B -> A -> B)')
            parser.add_argument('--lambda_identity', type=float, default=0.5, help='use identity mapping. Setting lambda_identity other than 0 has an effect of scaling the weight of the identity mapping loss. For example, if the weight of the identity loss should be 10 times smaller than the weight of the reconstruction loss, please set lambda_identity = 0.1')
        return parser

    def __init__(self, opt):
        super(UnpairedRevGANModel, self).__init__(opt)

        # specify the training losses you want to print out. The program will call base_model.get_current_losses
        self.loss_names = ['D_A', 'G_A', 'cycle_A', 'idt_A', 'D_B', 'G_B', 'cycle_B', 'idt_B']
        # specify the images you want to save/display. The program will call base_model.get_current_visuals
        visual_names_A = ['real_A', 'fake_B', 'rec_A']
        visual_names_B = ['real_B', 'fake_A', 'rec_B']
        if self.is_train and self.opt.lambda_identity > 0.0:
            visual_names_A.append('idt_A')
            visual_names_B.append('idt_B')

        self.visual_names = visual_names_A + visual_names_B
        # specify the models you want to save to the disk. The program will call base_model.save_networks and base_model.load_networks
        if self.is_train:
            self.model_names = ['G_A_enc', 'G_core', 'G_A_dec', 'G_B_enc', 'G_B_dec', 'D_A', 'D_B']
        else:  # during test time, only load Gs
            self.model_names = ['G_A_enc', 'G_core', 'G_A_dec', 'G_B_enc', 'G_B_dec']

        # load/define networks
        # The naming conversion is different from those used in the paper
        # Code (paper): G_A (G), G_B (F), D_A (D_Y), D_B (D_X)
        self.netG_A_enc = networks.define_G_enc(opt.input_nc, opt.output_nc,
                                                opt.ngf, opt.which_model_netG, opt.norm, 
                                                opt.init_type, opt.init_gain, self.gpu_ids, n_downsampling=opt.n_downsampling)

        self.netG_core = networks.define_G_core(opt.input_nc, opt.output_nc,
                                                opt.ngf, opt.which_model_netG, opt.norm, opt.use_naive,
                                                opt.init_type, opt.init_gain, self.gpu_ids, invertible=True, n_downsampling=opt.n_downsampling,
                                                coupling=opt.coupling)

        self.netG_A_dec = networks.define_G_dec(opt.input_nc, opt.output_nc,
                                                opt.ngf, opt.which_model_netG, opt.norm, 
                                                opt.init_type, opt.init_gain, self.gpu_ids, not opt.no_output_tanh, n_downsampling=opt.n_downsampling)

        self.netG_B_enc = networks.define_G_enc(opt.output_nc, opt.input_nc,
                                                opt.ngf, opt.which_model_netG, opt.norm, 
                                                opt.init_type, opt.init_gain, self.gpu_ids, n_downsampling=opt.n_downsampling)

        self.netG_B_dec = networks.define_G_dec(opt.output_nc, opt.input_nc,
                                                opt.ngf, opt.which_model_netG, opt.norm, 
                                                opt.init_type, opt.init_gain, self.gpu_ids, not opt.no_output_tanh, n_downsampling=opt.n_downsampling)

        if self.is_train:
            use_sigmoid = opt.no_lsgan
            self.netD_A = networks.define_D(opt.output_nc, opt.ndf, opt.which_model_netD,
                                            opt.n_layers_D, opt.norm, use_sigmoid, opt.init_type, opt.init_gain, self.gpu_ids)
            self.netD_B = networks.define_D(opt.input_nc, opt.ndf, opt.which_model_netD,
                                            opt.n_layers_D, opt.norm, use_sigmoid, opt.init_type, opt.init_gain, self.gpu_ids)

        if self.is_train:
            self.fake_A_pool = ImagePool(opt.pool_size)
            self.fake_B_pool = ImagePool(opt.pool_size)
            # define loss functions
            self.criterionGAN = networks.GANLoss(use_lsgan=not opt.no_lsgan).to(self.device)
            self.criterionCycle = torch.nn.L1Loss()
            self.criterionIdt = torch.nn.L1Loss()
            # initialize optimizers
            self.params_G = [self.netG_A_enc.parameters(),
                             self.netG_core.parameters(),
                             self.netG_A_dec.parameters(),
                             self.netG_B_enc.parameters(),
                             self.netG_B_dec.parameters()]
            self.optimizer_G = torch.optim.Adam(itertools.chain(*self.params_G
                                                ),
                                                lr=opt.lr_G, betas=(opt.beta1, 0.999))
            self.params_D = [self.netD_A.parameters(),
                             self.netD_B.parameters()]
            self.optimizer_D = torch.optim.Adam(itertools.chain(*self.params_D),
                                                lr=opt.lr_D, betas=(opt.beta1, 0.999))
            self.optimizers = []
            self.optimizers.append(self.optimizer_G)
            self.optimizers.append(self.optimizer_D)

    def set_input(self, input):
        AtoB = self.opt.direction == 'AtoB'
        self.real_A = input['A' if AtoB else 'B'].to(self.device)
        self.real_B = input['B' if AtoB else 'A'].to(self.device)
        self.image_paths = input['A_paths' if AtoB else 'B_paths']

    def forward(self):
        torch.cuda.empty_cache()

        # Forward cycle (A to B)
        self.fake_B = self.netG_A_dec(self.netG_core(self.netG_A_enc(self.real_A)))
        self.rec_A = self.netG_B_dec(self.netG_core(self.netG_B_enc(self.fake_B), inverse=True))

        # Backward cycle (B to A)
        self.fake_A = self.netG_B_dec(self.netG_core(self.netG_B_enc(self.real_B), inverse=True))
        self.rec_B = self.netG_A_dec(self.netG_core(self.netG_A_enc(self.fake_A)))

    def backward_D_basic(self, netD, real, fake):
        # Real
        pred_real = netD(real)
        loss_D_real = self.criterionGAN(pred_real, True)
        # Fake
        pred_fake = netD(fake.detach())
        loss_D_fake = self.criterionGAN(pred_fake, False)
        # Combined loss
        loss_D = (loss_D_real + loss_D_fake) * 0.5
        # backward
        if self.opt.grad_reg > 0.0:
            loss_D.backward(retain_graph=True)
            Lgrad = torch.cat([x.grad.view(-1) for x in netD.parameters()])
            loss_D = loss_D - self.opt.grad_reg * (0.5 * torch.norm(Lgrad) / len(Lgrad))
        else:
            loss_D.backward(retain_graph=True)
        return loss_D

    def backward_D_A(self):
        fake_B = self.fake_B_pool.query(self.fake_B)
        self.loss_D_A = self.backward_D_basic(self.netD_A, self.real_B, fake_B)

    def backward_D_B(self):
        fake_A = self.fake_A_pool.query(self.fake_A)
        self.loss_D_B = self.backward_D_basic(self.netD_B, self.real_A, fake_A)
        

    def backward_G(self):
        lambda_idt = self.opt.lambda_identity
        lambda_A = self.opt.lambda_A
        lambda_B = self.opt.lambda_B
        # Identity loss
        if lambda_idt > 0:
            # G_A should be identity if real_B is fed.
            self.idt_A = self.netG_A_dec(self.netG_core(self.netG_A_enc(self.real_B)))
            self.loss_idt_A = self.criterionIdt(self.idt_A, self.real_B) * lambda_B * lambda_idt
            # G_B should be identity if real_A is fed.
            self.idt_B = self.netG_B_dec(self.netG_core(self.netG_B_enc(self.real_A), inverse=True))
            self.loss_idt_B = self.criterionIdt(self.idt_B, self.real_A) * lambda_A * lambda_idt
        else:
            self.loss_idt_A = 0
            self.loss_idt_B = 0

        # GAN loss D_A(G_A(A))
        self.loss_G_A = self.criterionGAN(self.netD_A(self.fake_B), True)
        if self.opt.grad_reg > 0.0:
            self.loss_G_A.backward(retain_graph=True)
            Lgrad = torch.cat([x.grad.view(-1) for x in [*self.netG_A_enc.parameters(),
                                                         *self.netG_core.parameters(),
                                                         *self.netG_A_dec.parameters()]])
            self.loss_G_A = self.loss_G_A - self.opt.grad_reg * (0.5 * torch.norm(Lgrad) / len(Lgrad))

        # GAN loss D_B(G_B(B))
        self.loss_G_B = self.criterionGAN(self.netD_B(self.fake_A), True)
        if self.opt.grad_reg > 0.0:
            self.loss_G_B.backward(retain_graph=True)
            Lgrad = torch.cat([x.grad.view(-1) for x in [*self.netG_B_enc.parameters(),
                                                         *self.netG_core.parameters(),
                                                         *self.netG_B_dec.parameters()]])
            self.loss_G_B = self.loss_G_B - self.opt.grad_reg * (0.5 * torch.norm(Lgrad) / len(Lgrad))

        # Forward cycle loss
        self.loss_cycle_A = self.criterionCycle(self.rec_A, self.real_A) * lambda_A
        # Backward cycle loss
        self.loss_cycle_B = self.criterionCycle(self.rec_B, self.real_B) * lambda_B

        # combined loss
        self.loss_G = self.loss_G_A + self.loss_G_B + self.loss_cycle_A + self.loss_cycle_B + self.loss_idt_A + self.loss_idt_B
        if self.opt.grad_reg > 0.0:
            self.loss_G.backward(retain_graph=True)
            Lgrad = torch.cat([torch.cat([x.grad.view(-1) for x in y]) for y in [
                                 self.netG_A_enc.parameters(),
                                 self.netG_B_enc.parameters(),
                                 self.netG_core.parameters(),
                                 self.netG_A_dec.parameters(),
                                 self.netG_B_dec.parameters()]])
            self.loss_G = self.loss_G - self.opt.grad_reg * (0.5 * torch.norm(Lgrad) / len(Lgrad))
            self.loss_G.backward()
        else:
            self.loss_G.backward()

    def optimize_parameters(self):
        # forward
        self.forward()
        # G_A and G_B
        self.set_requires_grad([self.netD_A, self.netD_B], False)
        self.optimizer_G.zero_grad()
        self.backward_G()
        self.optimizer_G.step()
        # D_A and D_B
        self.set_requires_grad([self.netD_A, self.netD_B], True)
        self.optimizer_D.zero_grad()
        self.backward_D_A()
        self.backward_D_B()
        self.optimizer_D.step()
