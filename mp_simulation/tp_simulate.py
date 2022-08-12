import torch
import sys
import numpy as np
import argparse
import torch.distributed as dist
from groups import MPU
import time

@torch.jit.script
def bias_gelu(bias, y):
    x = bias + y
    return  x * 0.5 * (1.0 + torch.tanh(0.79788456 * x * (1 + 0.044715 * x * x)))

@torch.jit.script
def bias_gelu_back(g, bias, y):
    x = bias + y
    tanh_out = torch.tanh(0.79788456 * x * (1 + 0.044715 * x * x))
    # sqrt(2/pi) * 3 * 0.044715 -> 0.1070322243
    ff = 0.5 * x * ((1 - tanh_out * tanh_out) * (0.79788456 + 0.1070322243 * x * x)) + 0.5 * (1 + tanh_out)
    return ff*g

def allreduce(x, mpu, op=dist.ReduceOp.SUM):
    if mpu is not None:
        dist.all_reduce(x, op=op, group=mpu.get_model_parallel_group())
    return x

def reduce_scatter(x):
    raise NotImplementedError
    x = allreduce(x)
    tp_rank = dist.get_rank()
    tp_size = dist.get_world_size()
    size = x.shape[0] // tp_size
    input_list = list(torch.split(x, size, dim=0))
    output = input_list[tp_rank]
    return output

def allgather(x):
    raise NotImplementedError
    x = allreduce(x) 
    return x


class GeLUFunction(torch.autograd.Function):
    @staticmethod
    # bias is an optional argument
    def forward(ctx, input, bias):
        ctx.save_for_backward(input, bias)
        return bias_gelu(bias, input)

    @staticmethod
    def backward(ctx, grad_output):
        input, bias = ctx.saved_tensors
        tmp = bias_gelu_back(grad_output, bias, input)
        return tmp, tmp

class CPL(torch.nn.Module):
    def __init__(self, hsize, tp_size, modify=False):
        super().__init__()
        #assert 4*hsize % tp_size == 0
        if not modify:
            self.fc = torch.nn.Linear(hsize, 4*hsize//tp_size, bias=False)
        else:
            self.weight = torch.nn.Parameter(torch.randn(hsize, 4*hsize//tp_size))
        self.bias = torch.nn.Parameter(torch.zeros(4*hsize//tp_size))
        self.modify = modify

    def forward(self, x):
        if not self.modify:
            return self.fc(x), self.bias
        else:
            x_ = x.view(-1, x.shape[-1])
            y = torch.matmul(x_, self.weight)
            return y.view(x.shape[0], x.shape[1], -1), self.bias

class RPL(torch.nn.Module):
    def __init__(self, hsize, tp_size, modify=False, reduce_scatter=False):
        super().__init__()
#        assert 4*hsize % tp_size == 0
        #assert tp_size == 1
        if not modify:
            self.fc = torch.nn.Linear(4*hsize//tp_size, hsize, bias=False)
        else:
            self.weight = torch.nn.Parameter(torch.randn(4*hsize//tp_size, hsize))
        #if not reduce_scatter:
        self.bias = torch.nn.Parameter(torch.zeros(hsize))
        #else:
        #    self.bias = torch.nn.Parameter(torch.zeros(hsize//tp_size))
        self.modify = modify
        self.reduce_scatter = reduce_scatter
        self.tp_size = tp_size

    def forward(self, x):
        if not self.modify:
            out = self.fc(x)
        else:
            x_ = x.view(-1, x.shape[-1])
            y = torch.matmul(x_, self.weight)
            out  = y.view(x.shape[0], x.shape[1], -1)
        if self.reduce_scatter:
            return out, self.bias 
        else:
            return out, self.bias

class ParallelMLP(torch.nn.Module):
    def __init__(self, hsize, mpu, modify_cpl=False, modify_rpl=False, reduce_scatter=False):
        super().__init__()
        if mpu:
            tp_size = mpu.get_model_parallel_world_size()
        else:
            tp_size = 1

        #assert tp_size == 1
        self.cpl = CPL(hsize, tp_size, modify_cpl)
        self.rpl = RPL(hsize, tp_size, modify_rpl, reduce_scatter)

        self.cpl_start = torch.cuda.Event(enable_timing=True)
        self.cpl_stop = torch.cuda.Event(enable_timing=True)

        self.cpl_bias_start = torch.cuda.Event(enable_timing=True)
        self.cpl_bias_stop = torch.cuda.Event(enable_timing=True)

        self.rpl_start = torch.cuda.Event(enable_timing=True)
        self.rpl_stop = torch.cuda.Event(enable_timing=True)

        self.rpl_bias_start = torch.cuda.Event(enable_timing=True)
        self.rpl_bias_stop = torch.cuda.Event(enable_timing=True)
        
        self.comm_start = torch.cuda.Event(enable_timing=True)
        self.comm_stop = torch.cuda.Event(enable_timing=True)

        self.reduce_scatter = reduce_scatter
        self.mpu = mpu
        #assert self.mpu is None

    def forward(self, x):
        self.cpl_start.record()
        
        out, bias = self.cpl(x)
        
        self.cpl_bias_start.record()
        out = GeLUFunction.apply(out, bias)
        self.cpl_bias_stop.record()
        
        self.cpl_stop.record()
        

        self.rpl_start.record()
        out, bias = self.rpl(out)
        self.comm_start.record()
        if self.reduce_scatter:
            prev_out = out
            out = reduce_scatter(out, self.mpu)
        else:
            out = allreduce(out, self.mpu)
        self.comm_stop.record()

        self.rpl_bias_start.record()
        if args.reduce_scatter:
            out = torch.add(out, bias, alpha=1/tp)
        else:
            out = out + bias
        self.rpl_bias_stop.record()
        
        if args.reduce_scatter:
            out = allgather(prev_out, self.mpu)
        self.rpl_stop.record()
        
        return out

    def profile_stats(self):
        rpl_time = self.rpl_start.elapsed_time(self.rpl_stop)

        comm_time = self.comm_start.elapsed_time(self.comm_stop)

        rpl_bias_time = self.rpl_bias_start.elapsed_time(self.rpl_bias_stop)
        cpl_bias_time = self.cpl_bias_start.elapsed_time(self.cpl_bias_stop)

        cpl_time = self.cpl_start.elapsed_time(self.cpl_stop)

        total_time = self.cpl_start.elapsed_time(self.rpl_stop)

        #print(f"CPL time = {cpl_time:.2f} ms (matmul: {cpl_time-cpl_bias_time:.2f} ms bias: {cpl_bias_time:.2f}) | RPL time = {rpl_time:.2f} ms (matmul: {rpl_time-rpl_bias_time-comm_time:.2f} ms bias: {rpl_bias_time:.2f} ms comm: {comm_time:.2f} ms)  | Total time = {total_time:.2f} ms")
        return cpl_time, rpl_time, total_time

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--modify-rpl", action='store_true')
    parser.add_argument("--modify-cpl", action='store_true')
    parser.add_argument("--hsize", type=int)
    parser.add_argument("--mbs", type=int, default=2)
    parser.add_argument("--reduce-scatter", action='store_true')
    parser.add_argument("--tp-size", type=int)
    parser.add_argument("--ep-size", type=int)
    parser.add_argument("--enable-expert-tensor-parallelism", action='store_true')
    parser.add_argument("--drop-tokens", choices=["None", "local", "global", "global-opt"], default="None")

    args = parser.parse_args()

    assert not args.reduce_scatter, "not sure if this is correct"
    assert not (args.enable_expert_tensor_parallelism and args.ep_size > 1), "expert tp not supported yet"


    dist.init_process_group(backend="nccl")
    mpu = MPU(args.tp_size, args.ep_size, optim_a2a=(args.drop_tokens=='global-opt'))
    tp_size = mpu.get_model_parallel_world_size()
    torch.cuda.set_device(dist.get_rank() % torch.cuda.device_count())

    mbs = args.mbs
    modify_rpl=args.modify_rpl
    modify_cpl=args.modify_cpl

    hsize=args.hsize
    tp=tp_size

    sq_len=2048
    flops = 16 * mbs * sq_len * hsize ** 2 / tp
    #if not args.enable_expert_tensor_parallelism:
    #    flops = flops * tp

    num_batches = 80
    num_gpus = dist.get_world_size()

    
    if args.enable_expert_tensor_parallelism:
        expert_mpu = mpu
    else:
        expert_mpu = None
    expert = ParallelMLP(hsize, 
            expert_mpu,
            modify_cpl=args.modify_cpl, 
            modify_rpl=args.modify_rpl, 
            reduce_scatter=args.reduce_scatter).cuda().half()

    assert sq_len * mbs % args.ep_size == 0
    batch = torch.randn(args.ep_size, sq_len*mbs//args.ep_size, hsize).cuda().half()
    batch_start = torch.cuda.Event(enable_timing=True)
    batch_end = torch.cuda.Event(enable_timing=True)
    a2a_start_1, a2a_start_2 = torch.cuda.Event(enable_timing=True), torch.cuda.Event(enable_timing=True)
    a2a_end_1, a2a_end_2 = torch.cuda.Event(enable_timing=True), torch.cuda.Event(enable_timing=True)

    all_gather_start = torch.cuda.Event(enable_timing=True)
    all_gather_end = torch.cuda.Event(enable_timing=True)

    ep_group_size = dist.get_world_size(group=mpu.get_expert_parallel_group())
    for iter_no in range(num_batches):
        batch_start.record()
        if args.ep_size > 1:
            if args.drop_tokens == "global":
                dropped_batch = drop_global(batch, mpu, dim=1)
            elif args.drop_tokens == "global-opt":
                dropped_batch = drop_global_opt(batch, mpu, dim=0)
            else:
                dropped_batch = batch
            output = torch.empty_like(dropped_batch)
            a2a_start_1.record()
            if ep_group_size > 1:
                dist.all_to_all_single(output, dropped_batch.contiguous(), group=mpu.get_expert_parallel_group())
            a2a_end_1.record()
            if args.drop_tokens == "local":
                output = drop_local(output, mpu, dim=1)
        else:
            output = batch
        with torch.no_grad():
            output = expert(output)
            en = time.time()
        if args.ep_size > 1:
            if args.drop_tokens == "local":
                output = gather_local(output, mpu, dim=1)
            new_out = torch.empty_like(output)

            a2a_start_2.record()
            if ep_group_size > 1:
                dist.all_to_all_single(new_out, output.contiguous(), group=mpu.get_expert_parallel_group())
            a2a_end_2.record()
            output = new_out
            if args.drop_tokens == "global":
                all_gather_start.record()
                output = gather_global(output, mpu, dim=1)
                all_gather_end.record()
            elif args.drop_tokens == "global-opt":
                all_gather_start.record()
                output = gather_global_opt(output, mpu, dim=0)
                all_gather_end.record()
        batch_end.record()
        torch.cuda.synchronize()
        batch_time = batch_start.elapsed_time(batch_end) / 1000
        all_gather_time = 0
        if args.drop_tokens == "global" or args.drop_tokens == "global-opt":
            all_gather_time = all_gather_start.elapsed_time(all_gather_end)
        if ep_group_size > 1:
            a2a_time = a2a_start_1.elapsed_time(a2a_end_1) + a2a_start_2.elapsed_time(a2a_end_2)
        else:
            a2a_time = 0
        tflops_per_gpu = flops / 1e12 / batch_time
        if dist.get_rank() == 0:
            cpl_time, rpl_time, expert_time = expert.profile_stats()
            cpl_tflops = flops * 1000 / 2 / 1e12 / cpl_time
            rpl_tflops = flops * 1000 / 2 / 1e12 / rpl_time
            overhead = batch_time*1000 - expert_time - all_gather_time - a2a_time
            print(f"Iteration {iter_no+1}: TFLOPs: {tflops_per_gpu:.2f} | CPL TFLOPs: {cpl_tflops:.2f} | RPL TFLOPs: {rpl_tflops:.2f}")
            #print(f"Iteration {iter_no+1} : TFLOPs: {tflops_per_gpu:.2f} | A2A time = {a2a_time:.2f} ms | All-Gath Time = {all_gather_time:.2f}  ms | Expert Time = {expert_time:.2f} ms | Overhead = {overhead:.2f} ms | Total time = {batch_time*1000:.2f} ms")
