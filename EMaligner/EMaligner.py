import numpy as np
import renderapi
import argschema
from .schemas import EMA_Schema
import utils
from .transform.transform import AlignerTransform
import time
import scipy.sparse as sparse
from scipy.sparse import csr_matrix
from scipy.sparse.linalg import factorized
import warnings
import os
import sys
import multiprocessing
import logging
import json
warnings.simplefilter(action='ignore', category=FutureWarning)
import h5py
warnings.resetwarnings()

logger = logging.getLogger(__name__)


def calculate_processing_chunk(fargs):
    # set up for calling using multiprocessing pool
    [pair, zloc, args, tile_ids] = fargs

    dbconnection = utils.make_dbconnection(args['pointmatch'])
    sorter = np.argsort(tile_ids)

    # this dict will get returned
    chunk = {}
    chunk['tiles_used'] = np.zeros(tile_ids.size).astype(bool)
    chunk['data'] = None
    chunk['indices'] = None
    chunk['indptr'] = None
    chunk['weights'] = None
    chunk['nchunks'] = 0
    chunk['zlist'] = []

    pstr = '  proc%d: ' % zloc

    # get point matches
    t0 = time.time()
    matches = utils.get_matches(
            pair['section1'],
            pair['section2'],
            args['pointmatch'],
            dbconnection)

    if len(matches) == 0:
        return chunk

    # extract IDs for fast checking
    pids = np.array([m['pId'] for m in matches])
    qids = np.array([m['qId'] for m in matches])

    # remove matches that don't have both IDs in tile_ids
    instack = np.in1d(pids, tile_ids) & np.in1d(qids, tile_ids)
    matches = np.array(matches)[instack].tolist()
    pids = pids[instack]
    qids = qids[instack]

    if len(matches) == 0:
        logger.debug(
                "%sno tile pairs in "
                "stack for pointmatch groupIds %s and %s" % (
                    pstr, pair['section1'], pair['section2']))
        return chunk

    logger.debug(
            "%sloaded %d matches, using %d, "
            "for groupIds %s and %s in %0.1f sec "
            "using interface: %s" % (
                pstr,
                instack.size,
                len(matches),
                pair['section1'],
                pair['section2'],
                time.time() - t0,
                args['pointmatch']['db_interface']))

    t0 = time.time()
    # for the given point matches, these are the indices in tile_ids
    # these determine the column locations in A for each tile pair
    # this is a fast version of np.argwhere() loop
    pinds = sorter[np.searchsorted(tile_ids, pids, sorter=sorter)]
    qinds = sorter[np.searchsorted(tile_ids, qids, sorter=sorter)]

    # conservative pre-allocation of the arrays we need to populate
    # will truncate at the end
    nmatches = len(matches)
    transform = AlignerTransform(
        args['transformation'],
        fullsize=args['fullsize_transform'],
        order=args['poly_order'])
    nd = (
        transform.nnz_per_row *
        transform.rows_per_ptmatch *
        args['matrix_assembly']['npts_max'] *
        nmatches)
    ni = (
        transform.rows_per_ptmatch *
        args['matrix_assembly']['npts_max'] *
        nmatches)
    data = np.zeros(nd).astype('float64')
    indices = np.zeros(nd).astype('int64')
    indptr = np.zeros(ni + 1).astype('int64')
    weights = np.zeros(ni).astype('float64')

    # see definition of CSR format, wikipedia for example
    indptr[0] = 0

    # track how many rows
    nrows = 0

    tilepair_weightfac = tilepair_weight(
            pair['z1'],
            pair['z2'],
            args['matrix_assembly'])

    for k in np.arange(nmatches):
        # create the CSR sub-matrix for this tile pair
        d, ind, iptr, wts, npts = transform.CSR_from_tilepair(
            matches[k],
            pinds[k],
            qinds[k],
            args['matrix_assembly']['npts_min'],
            args['matrix_assembly']['npts_max'],
            args['matrix_assembly']['choose_random'])

        if d is None:
            continue  # if npts<nmin, or all weights=0

        # note both as used
        chunk['tiles_used'][pinds[k]] = True
        chunk['tiles_used'][qinds[k]] = True

        # add sub-matrix to global matrix
        global_dind = np.arange(
            npts *
            transform.rows_per_ptmatch *
            transform.nnz_per_row) + \
            nrows*transform.nnz_per_row
        data[global_dind] = d
        indices[global_dind] = ind

        global_rowind = \
            np.arange(npts * transform.rows_per_ptmatch) + nrows
        weights[global_rowind] = wts * tilepair_weightfac
        indptr[global_rowind + 1] = iptr + indptr[nrows]

        nrows += wts.size

    del matches
    # truncate, because we allocated conservatively
    data = data[0: nrows * transform.nnz_per_row]
    indices = indices[0: nrows * transform.nnz_per_row]
    indptr = indptr[0: nrows + 1]
    weights = weights[0: nrows]

    chunk['data'] = np.copy(data)
    chunk['weights'] = np.copy(weights)
    chunk['indices'] = np.copy(indices)
    chunk['indptr'] = np.copy(indptr)
    chunk['zlist'].append(pair['z1'])
    chunk['zlist'].append(pair['z2'])
    chunk['zlist'] = np.array(chunk['zlist'])
    del data, indices, indptr, weights

    return chunk


def tilepair_weight(z1, z2, matrix_assembly):
    if matrix_assembly['explicit_weight_by_depth'] is not None:
        ind = matrix_assembly['depth'].index(int(np.abs(z1 - z2)))
        tp_weight = matrix_assembly['explicit_weight_by_depth'][ind]
    else:
        if z1 == z2:
            tp_weight = matrix_assembly['montage_pt_weight']
        else:
            tp_weight = matrix_assembly['cross_pt_weight']
            if matrix_assembly['inverse_dz']:
                tp_weight = tp_weight/(np.abs(z2 - z1) + 1)
    return tp_weight


def mat_stats(m, name):
    shape = m.get_shape()
    mesg = "\n matrix: %s\n" % name
    mesg += " format: %s\n" % m.getformat()
    mesg += " shape: (%d, %d)\n" % (shape[0], shape[1])
    mesg += " nnz: %d" % m.nnz
    logger.debug(mesg)


class EMaligner(argschema.ArgSchemaParser):
    default_schema = EMA_Schema

    def run(self):
        logger.setLevel(self.args['log_level'])
        t0 = time.time()
        zvals = np.arange(
            self.args['first_section'],
            self.args['last_section'] + 1)

        ingestconn = None
        # make a connection to the new stack
        if self.args['output_mode'] == 'stack':
            ingestconn = utils.make_dbconnection(self.args['output_stack'])
            renderapi.stack.create_stack(
                self.args['output_stack']['name'][0],
                render=ingestconn)

        # montage
        if self.args['solve_type'] == 'montage':
            # check for zvalues in stack
            tmp = self.args['input_stack']['db_interface']
            self.args['input_stack']['db_interface'] = 'render'
            conn = utils.make_dbconnection(self.args['input_stack'])
            self.args['input_stack']['db_interface'] = tmp
            z_in_stack = renderapi.stack.get_z_values_for_stack(
                self.args['input_stack']['name'][0],
                render=conn)
            newzvals = []
            for z in zvals:
                if z in z_in_stack:
                    newzvals.append(z)
            zvals = np.array(newzvals)
            for z in zvals:
                self.results = self.assemble_and_solve(
                    np.array([z]),
                    ingestconn)
        # 3D
        elif self.args['solve_type'] == '3D':
            self.results = self.assemble_and_solve(zvals, ingestconn)

        if ingestconn is not None:
            if self.args['close_stack']:
                renderapi.stack.set_stack_state(
                    self.args['output_stack']['name'][0],
                    state='COMPLETE',
                    render=ingestconn)
        logger.info(' total time: %0.1f' % (time.time() - t0))

    def assemble_and_solve(self, zvals, ingestconn):
        t0 = time.time()

        #self.transform = AlignerTransform(
        #    name=self.args['transformation'],
        #    order=self.args['poly_order'],
        #    fullsize=self.args['fullsize_transform'])

        if self.args['ingest_from_file'] != '':
            assemble_result = self.assemble_from_hdf5(
                self.args['ingest_from_file'],
                zvals,
                read_data=False)
            x = assemble_result['tforms']
            results = {}

        else:
            # assembly
            if self.args['assemble_from_file'] != '':
                assemble_result = self.assemble_from_hdf5(
                    self.args['assemble_from_file'],
                    zvals)
            else:
                assemble_result = self.assemble_from_db(zvals)

            if assemble_result['A'] is not None:
                mat_stats(assemble_result['A'], 'A')

            self.ntiles_used = np.count_nonzero(assemble_result['tiles_used'])
            logger.info(' A created in %0.1f seconds' % (time.time() - t0))

            if self.args['profile_data_load']:
                raise EMalignerException(
                    "exiting after timing profile")

            # solve
            message, results = \
                self.solve_or_not(
                    assemble_result['A'],
                    assemble_result['weights'],
                    assemble_result['reg'],
                    assemble_result['x'])
            logger.info('\n' + message)
            if assemble_result['A'] is not None:
                results['Ashape'] = assemble_result['A'].shape
            del assemble_result['A']

        if self.args['output_mode'] == 'stack':
            solved_resolved = utils.update_tilespecs(
                    assemble_result['resolved'], results['x'], assemble_result['tiles_used'])
            utils.write_to_new_stack(
                    solved_resolved,
                    self.args['output_stack']['name'][0],
                    ingestconn,
                    self.args['render_output'],
                    self.args['output_stack']['use_rest'],
                    self.args['overwrite_zlayer'])
            if self.args['render_output'] == 'stdout':
                logger.info(message)
        del assemble_result['shared_tforms'], assemble_result['tspecs'], assemble_result['x']

        return results

    assemble_struct = {
        'A': None,
        'weights': None,
        'reg': None,
        'tspecs': None,
        'tforms': None,
        'tids': None,
        'shared_tforms': None,
        'unused_tids': None}

    def assemble_from_hdf5(self, filename, zvals, read_data=True):
        assemble_result = dict(self.assemble_struct)

        from_stack = get_tileids_and_tforms(
            self.args['input_stack'],
            self.args['transformation'],
            zvals,
            fullsize=self.args['fullsize_transform'],
            order=self.args['poly_order'])

        assemble_result['shared_tforms'] = from_stack.pop('shared_tforms')

        with h5py.File(filename, 'r') as f:
            assemble_result['tids'] = np.array(
                f.get('used_tile_ids')[()]).astype('U')
            assemble_result['unused_tids'] = np.array(
                f.get('unused_tile_ids')[()]).astype('U')
            k = 0
            assemble_result['tforms'] = []
            while True:
                name = 'transforms_%d' % k
                if name in f.keys():
                    assemble_result['tforms'].append(f.get(name)[()])
                    k += 1
                else:
                    break

            if len(assemble_result['tforms']) == 1:
                n = assemble_result['tforms'][0].size
                assemble_result['tforms'] = np.array(
                    assemble_result['tforms']).flatten().reshape((n, 1))
            else:
                assemble_result['tforms'] = np.transpose(
                    np.array(assemble_result['tforms']))

            reg = f.get('lambda')[()]
            datafile_names = f.get('datafile_names')[()]
            file_args = json.loads(f.get('input_args')[()][0])

        # get the tile IDs and transforms
        tile_ind = np.in1d(from_stack['tids'], assemble_result['tids'])
        assemble_result['tspecs'] = from_stack['tspecs'][tile_ind]

        outr = sparse.eye(reg.size, format='csr')
        outr.data = reg
        assemble_result['reg'] = outr

        if read_data:
            data = np.array([]).astype('float64')
            weights = np.array([]).astype('float64')
            indices = np.array([]).astype('int64')
            indptr = np.array([]).astype('int64')

            fdir = os.path.dirname(filename)
            i = 0
            for fname in datafile_names:
                with h5py.File(os.path.join(fdir, fname), 'r') as f:
                    data = np.append(data, f.get('data')[()])
                    indices = np.append(indices, f.get('indices')[()])
                    if i == 0:
                        indptr = np.append(indptr, f.get('indptr')[()])
                        i += 1
                    else:
                        indptr = np.append(
                            indptr,
                            f.get('indptr')[()][1:] + indptr[-1])
                    weights = np.append(weights, f.get('weights')[()])
                    logger.info('  %s read' % fname)

            assemble_result['A'] = csr_matrix((data, indices, indptr))

            outw = sparse.eye(weights.size, format='csr')
            outw.data = weights
            assemble_result['weights'] = outw

        # alert about differences between this call and the original
        for k in file_args.keys():
            if k in self.args.keys():
                if file_args[k] != self.args[k]:
                    logger.warning("for key \"%s\" " % k)
                    logger.warning("  from file: " + str(file_args[k]))
                    logger.warning("  this call: " + str(self.args[k]))
            else:
                logger.warning("for key \"%s\" " % k)
                logger.warning("  file     : " + str(file_args[k]))
                logger.warning("  this call: not specified")

        logger.info("csr inputs read from files listed in : "
                    "%s" % self.args['assemble_from_file'])

        return assemble_result

    def assemble_from_db(self, zvals):
        assemble_result = dict(self.assemble_struct)

        assemble_result['resolved'] = utils.get_resolved_tilespecs(
            self.args['input_stack'],
            self.args['transformation'],
            self.args['n_parallel_jobs'],
            zvals,
            fullsize=self.args['fullsize_transform'],
            order=self.args['poly_order'])

        # create A matrix in compressed sparse row (CSR) format
        CSR_A = self.create_CSR_A(assemble_result['resolved'])
        assemble_result['A'] = CSR_A.pop('A')
        assemble_result['weights'] = CSR_A.pop('weights')
        assemble_result['tiles_used'] = CSR_A.pop('tiles_used')
        assemble_result['reg'] = CSR_A.pop('reg')
        assemble_result['x'] = CSR_A.pop('x')

        # some book-keeping if there were some unused tiles
        #tile_ind = np.in1d(from_stack['tids'], CSR_A['tiles_used'])
        #assemble_result['tspecs'] = from_stack['tspecs'][tile_ind]
        #assemble_result['tids'] = \
        #    from_stack['tids'][tile_ind]
        #assemble_result['unused_tids'] = \
        #    from_stack['tids'][np.invert(tile_ind)]

        # remove columns in A for unused tiles
        #slice_ind = np.repeat(
        #    tile_ind,
        #    self.transform.DOF_per_tile / from_stack['tforms'].shape[1])
        #if self.args['output_mode'] != 'hdf5':
        #    # for large matrices,
        #    # this might be expensive to perform on CSR format
        #    assemble_result['A'] = assemble_result['A'][:, slice_ind]

        #assemble_result['tforms'] = from_stack['tforms'][slice_ind, :]
        #del from_stack, CSR_A['tiles_used'], tile_ind

        # create the regularization vectors
        #assemble_result['reg'] = self.transform.create_regularization(
        #    assemble_result['tforms'].shape[0],
        #    self.args['regularization'])

        # output the regularization vectors to hdf5 file
        if self.args['output_mode'] == 'hdf5':
            write_reg_and_tforms(
                dict(self.args),
                CSR_A['metadata'],
                assemble_result['tforms'],
                assemble_result['reg'],
                assemble_result['tids'],
                assemble_result['unused_tids'])

        return assemble_result


    def concatenate_chunks(self, chunks):
        i = 0
        while chunks[i]['data'] is None:
            if i == len(chunks) - 1:
                break
            i += 1
        c0 = chunks[i]
        for c in chunks[(i + 1):]:
            if c['data'] is not None:
                for ckey in ['data', 'weights', 'indices', 'zlist']:
                    c0[ckey] = np.append(c0[ckey], c[ckey])
                ckey = 'indptr'
                lastptr = c0[ckey][-1]
                c0[ckey] = np.append(c0[ckey], c[ckey][1:] + lastptr)
        return c0


    def concatenate_results(self, results):
        result = {}
        result['data'] = np.concatenate([
            results[i]['data'] for i in range(len(results))
            if results[i]['data'] is not None]).astype('float64')
        result['weights'] = np.concatenate([
            results[i]['weights'] for i in range(len(results))
            if results[i]['data'] is not None]).astype('float64')
        result['indices'] = np.concatenate([
            results[i]['indices'] for i in range(len(results))
            if results[i]['data'] is not None]).astype('int64')
        result['zlist'] = np.concatenate([
            results[i]['zlist'] for i in range(len(results))
            if results[i]['data'] is not None])
        # Pointers need to be handled differently,
        # since you need to sum the arrays
        result['indptr'] = [results[i]['indptr']
                  for i in range(len(results))
                  if results[i]['data'] is not None]
        indptr_cumends = np.cumsum([i[-1] for i in result['indptr']])
        result['indptr'] = np.concatenate(
            [j if i == 0 else j[1:]+indptr_cumends[i-1] for i, j
             in enumerate(result['indptr'])]).astype('int64')

        return result


    def create_CSR_A(self, resolved):
        func_result = {
            'A': None,
            'x': None,
            'reg': None,
            'weights': None,
            'tiles_used': None,
            'metadata': None}

        pool = multiprocessing.Pool(self.args['n_parallel_jobs'])

        pairs = utils.determine_zvalue_pairs(
                resolved,
                self.args['matrix_assembly']['depth'])

        npairs = len(pairs)
        tile_ids = np.array([t.tileId for t in resolved.tilespecs])
        fargs = [[pairs[i], i, self.args, tile_ids] for i in range(npairs)]

        with renderapi.client.WithPool(self.args['n_parallel_jobs']) as pool:
            results = np.array(pool.map(calculate_processing_chunk, fargs))

        func_result['tiles_used'] = results[0]['tiles_used']
        for result in results[1:]:
            func_result['tiles_used'] = \
                    func_result['tiles_used'] | result['tiles_used']

        if self.args['output_mode'] == 'hdf5':
            func_result['metadata'] = self.write_chunks_to_files(results)

        else:
            func_result['x'] = np.concatenate([
                t.tforms[-1].to_solve_vec() for t in resolved.tilespecs
                if t.tileId in tile_ids[func_result['tiles_used']]])
            reg = np.concatenate([
                t.tforms[-1].regularization(self.args['regularization']) for t in resolved.tilespecs
                if t.tileId in tile_ids[func_result['tiles_used']]])
            result = self.concatenate_results(results)
            A = csr_matrix((
                result['data'],
                result['indices'],
                result['indptr']))
            outw = sparse.eye(result['weights'].size, format='csr')
            outw.data = result['weights']
            func_result['reg'] = sparse.eye(reg.size, format='csr')
            func_result['reg'].data = reg
            tile_ind = np.in1d(tile_ids, func_result['tiles_used'])
            slice_ind = np.concatenate(
                    [np.repeat(func_result['tiles_used'][i], resolved.tilespecs[i].tforms[-1].DOF_per_tile)
                     for i in range(tile_ids.size)])
            func_result['A'] = A[:, slice_ind]
            func_result['weights'] = outw

        return func_result


    def solve_or_not(self, A, weights, reg, x0):
        t0 = time.time()
        # not
        if self.args['output_mode'] in ['hdf5']:
            message = '*****\nno solve for file output\n'
            message += 'solve from the files you just wrote:\n\n'
            message += 'python '
            for arg in sys.argv:
                message += arg+' '
            message = message + '--assemble_from_file ' + \
                self.args['hdf5_options']['output_dir']
            message = message + ' --output_mode none'
            message += '\n\nor, run it again to solve with no output:\n\n'
            message += 'python '
            for arg in sys.argv:
                message += arg + ' '
            message = message.replace(' hdf5 ', ' none ')
            x = None
            results = None
        else:
            results = utils.solve(A, weights, reg, x0)
            message = utils.message_from_solve_results(results)

            ## get the scales (quick way to look for distortion)
            #tforms = self.transform.from_solve_vec(x)
            #if isinstance(
            #        self.transform,
            #        renderapi.transform.Polynomial2DTransform):
            #    # renderapi does not have scale property
            #    if self.transform.order > 0:
            #        scales = np.array(
            #            [[t.params[0, 1], t.params[1, 2]]
            #             for t in tforms]).flatten()
            #    else:
            #        scales = np.array([0])
            #else:
            #    scales = np.array([
            #        np.array(t.scale) for t in tforms]).flatten()

            #results['scale'] = scales.mean()
            #message += '\n avg scale = %0.2f +/- %0.2f' % (
            #    scales.mean(), scales.std())

        return message, results


if __name__ == '__main__':
    mod = EMaligner(schema_type=EMA_Schema)
    mod.run()
