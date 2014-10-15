from __future__ import division

import argparse
import itertools
import os

import pyopencl as cl
import pyviennacl as vcl
import pyatidlas as atd

import tools
import optimize
import sys

from configobj import ConfigObj
from numpy import random
from dataset import generate_dataset
from model import train_model

DATATYPES = { 'single' : vcl.float32,
              'double' : vcl.float64 }

TYPES = { 'vector-axpy': {'template':atd.VectorAxpyTemplate,
                          'perf-index':lambda x: 3*x[0]*x[1][0]/x[2]*1e-9,
                          'perf-measure':'GB/s'},

          'matrix-axpy': {'template':atd.MatrixAxpyTemplate,
                          'perf-index':lambda x: 3*x[0]*x[1][0]*x[1][1]/x[2]*1e-9,
                          'perf-measure':'GB/s'},

          'reduction': {'template':atd.ReductionTemplate,
                        'perf-index':lambda x: 2*x[0]*x[1][0]/x[2]*1e-9,
                        'perf-measure':'GB/s'},

          'row-wise-reduction': {'template':atd.RowWiseReductionTemplate,
                                'perf-index':lambda x: x[0]*x[1][0]*x[1][1]/x[2]*1e-9,
                                'perf-measure':'GB/s'},

          'matrix-product': {'template': atd.MatrixProductTemplate,
                            'perf-index': lambda x: 2*x[1][0]*x[1][1]*x[1][2]/x[2]*1e-9,
                            'perf-measure': 'GFLOP/s'} }

def do_tuning(config_fname, viennacl_root):
    config = ConfigObj(config_fname)
    def map_to_list(T, x):
        return list(map(T, x if isinstance(x, list) else [x]))
    for operation in ['vector-axpy', 'matrix-axpy', 'reduction', 'row-wise-reduction', 'matrix-product']:
        if operation in config:
            p = config[operation]
            confdevices = p['devices']
            all_devices = [d for platform in cl.get_platforms() for d in platform.get_devices()]
            DEVICES_PRESETS = {'all': all_devices,
                               'gpus': [d for d in all_devices if d.type==cl.device_type.GPU],
                               'cpus': [d for d in all_devices if d.type==cl.device_type.CPU],
                               'accelerators': [d for d in all_devices if d.type==cl.device_type.ACCELERATOR]
            }
            devices = DEVICES_PRESETS[confdevices] if confdevices in DEVICES_PRESETS else [all_devices[int(i)] for i in confdevices]
            precisions =  map_to_list(str, p['precision'])
            if 'all' in precisions:
                precisions = ['single','double']
            datatypes = [DATATYPES[k] for k in precisions]
            #Iterate through the datatypes and the devices
            for datatype, device in itertools.product(datatypes, devices):
                ctx = cl.Context([device])
                ctx = vcl.backend.Context(ctx)
                device = ctx.current_device
                #Check data-type
                if datatype is vcl.float64 and not device.double_fp_config:
                    sys.stderr.write('Warning : The device ' + device.name + ' does not support double precision! Skipping ...')
                    continue
                #Helper for execution
                def execute(device, node, other_params, sizes, fname = os.devnull, parameters = None):
                    with vcl.Statement(node) as statement:
                        if parameters:
                            TemplateType = TYPES[operation]['template']
                            return tools.benchmark(TemplateType(TemplateType.Parameters(*parameters),*other_params), statement, device)
                        print('-----')
                        print(' '.join(map(str, ("Now tuning:", datatype.__name__, '-', operation, '-'.join(other_params), '[' + device.name, '(' + device.platform.name + ')] for sizes', sizes))))
                        with open(fname, "w+") as archive:
                            return optimize.genetic(statement, device, TYPES[operation]['template'], lambda p: TYPES[operation]['template'](p, *other_params),
                                                    lambda t: TYPES[operation]['perf-index']([datatype().itemsize, sizes, t]), TYPES[operation]['perf-measure'], archive)
                #Helper for tuning
                def tune(execution_handler, nTuning, nDataPoints, draw, additional_parameters):
                    if 'size' in p:
                        profile = execution_handler(map_to_list(int, p['size']))
                        if 'viennacl-src-root' in config:
                            tools.update_viennacl_headers(config['viennacl-src-root'],device,datatype,operation,additional_parameters,profile)
                    else:
                        def compute_perf(x, t):
                            return TYPES[operation]['perf-index']([datatype().itemsize, x, t])
                        X, Y, profiles = generate_dataset(TYPES[operation]['template'], execution_handler, nTuning, nDataPoints, draw)
                        train_model(X, Y, profiles, TYPES[operation]['perf-measure'])

                #Vector AXPY
                if operation=='vector-axpy':
                    def execution_handler(sizes, fname=os.devnull, parameters=None):
                        x = vcl.Vector(sizes[0], context=ctx, dtype=datatype)
                        y = vcl.Vector(sizes[0], context=ctx, dtype=datatype)
                        z = vcl.Vector(sizes[0], context=ctx, dtype=datatype)
                        return execute(device, vcl.Assign(z, vcl.ElementProd(vcl.exp(x + y),vcl.cos(x + y))), (), sizes, fname, parameters)
                    tune(execution_handler, 30, 1000, lambda : 64*np.random.randint(low=10, high=100000, size=1), ())
                #Reduction
                if operation=='reduction':
                    def execution_handler(sizes, fname=os.devnull, parameters=None):
                        x = vcl.Vector(sizes[0], context=ctx, dtype=datatype)
                        y = vcl.Vector(sizes[0], context=ctx, dtype=datatype)
                        s = vcl.Scalar(0, context=ctx, dtype=datatype)
                        return execute(device, vcl.Assign(s, vcl.Dot(x,y)), (), sizes, fname, parameters)
                    tune(execution_handler, 50, 1000, lambda : 64*np.random.randint(low=10, high=100000, size=1), ())
                #Matrix AXPY
                if operation=='matrix-axpy':
                    def execution_handler(sizes, fname=os.devnull, parameters=None):
                        A = vcl.Matrix(sizes, context=ctx, dtype=datatype)
                        B = vcl.Matrix(sizes, context=ctx, dtype=datatype)
                        C = vcl.Matrix(sizes, context=ctx, dtype=datatype)
                        return execute(device, vcl.Assign(C,A+B), (), sizes, fname, parameters)
                    tune(execution_handler, 50, 1000, lambda : 64*np.random.randint(low=5, high=100, size=2), ())
                #Row-wise reduction
                if operation=='row-wise-reduction':
                    layouts = map_to_list(str,p['layout'])
                    if 'all' in layouts:
                        layouts = ['N', 'T']
                    for A_trans in layouts:
                        def execution_handler(sizes, fname=os.devnull, parameters=None):
                            A = vcl.Matrix(sizes if A_trans=='N' else sizes[::-1], context=ctx, dtype=datatype, layout=vcl.COL_MAJOR)
                            x = vcl.Vector(sizes[1] if A_trans=='N' else sizes[0], context=ctx, dtype=datatype)
                            y = vcl.Vector(sizes[0] if A_trans=='N' else sizes[1], context=ctx, dtype=datatype)
                            LHS = A if A_trans=='N' else A.T
                            return execute(device, vcl.Assign(y, LHS*x), (), sizes, fname, parameters)
                        tune(execution_handler, 50, 1000, lambda : 64*np.random.randint(low=5, high=100, size=2), (A_trans,))
                #Matrix Product
                if operation=='matrix-product':
                    layouts = map_to_list(str,p['layout'])
                    if 'all' in layouts:
                        layouts = ['NN', 'NT', 'TN', 'TT']
                    for layout in layouts:
                        def execution_handler(sizes, fname=os.devnull, parameters=None):
                            A_trans = layout[0]
                            B_trans = layout[1]
                            A = vcl.Matrix((sizes[0], sizes[1]) if A_trans=='N' else (sizes[1],sizes[0]), context=ctx, dtype=datatype, layout=vcl.COL_MAJOR);
                            B = vcl.Matrix((sizes[1], sizes[2]) if B_trans=='N' else (sizes[2],sizes[1]), context=ctx, dtype=datatype, layout=vcl.COL_MAJOR);
                            LHS = A if A_trans=='N' else A.T
                            RHS = B if B_trans=='N' else B.T
                            alpha = vcl.HostScalar(1.0,  context=ctx, dtype = datatype)
                            beta = vcl.HostScalar(1.0, context=ctx, dtype = datatype)
                            C = vcl.Matrix((sizes[0], sizes[2]), context=ctx, dtype = datatype, layout=vcl.COL_MAJOR)
                            return execute(device, vcl.Assign(C,LHS*RHS*alpha + C*beta),(A_trans, B_trans), sizes, fname, parameters)
                        tune(execution_handler, 50, 2000, lambda : 64*np.random.randint(low=1, high=40, size=3),(layout[0], layout[1]))



if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest='action')
    print_devices_parser = subparsers.add_parser('list-devices', help='list the devices available')
    tune_parser = subparsers.add_parser('tune', help='tune using a specific configuration file')
    tune_parser.add_argument("--config", default="config.ini", required=False, type=str)
    tune_parser.add_argument("--viennacl-root", default='', required=False, type=str)
    args = parser.parse_args()

    if(args.action=='list-devices'):
        print("----------------")
        print("Devices available:")
        print("----------------")
        devices = [d for platform in cl.get_platforms() for d in platform.get_devices()]
        for (i, d) in enumerate(devices):
            print 'Device', i, '|',  cl.device_type.to_string(d.type), '|', d.name, 'on', d.platform.name
        print("----------------")
    else:
        print("------")
        print("Auto-tuning")
        print("------")
        do_tuning(args.config, args.viennacl_root)