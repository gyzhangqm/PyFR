# -*- coding: utf-8 -*-

import collections
import itertools as it

from mpi4py import MPI
import numpy as np
import pycuda.driver as cuda

import pyfr.backends.base as base
from pyfr.backends.cuda.util import memcpy2d_htod, memcpy2d_dtoh


class CUDAMatrixBase(base.MatrixBase):
    def __init__(self, backend, dtype, ioshape, initval, extent, tags):
        super(CUDAMatrixBase, self).__init__(backend, ioshape, tags)

        # Data type info
        self.dtype = dtype
        self.itemsize = np.dtype(dtype).itemsize

        # Alignment requirement for the leading dimension
        ldmod = backend.alignb // self.itemsize if 'align' in tags else 1

        # Matrix dimensions
        nrow, ncol = backend.compact_shape(ioshape)

        # Assign
        self.nrow, self.ncol = nrow, ncol
        self.leaddim = ncol - (ncol % -ldmod)
        self.leadsubdim = self.ioshape[-1]

        # Allocate
        backend.malloc(self, nrow*self.leaddim*self.itemsize, extent)

        # Retain the initial value
        self._initval = initval

    def onalloc(self, basedata, offset):
        self.basedata = int(basedata)
        self.data = self.basedata + offset
        self.offset = offset // self.itemsize

        # Process any initial value
        if self._initval is not None:
            self.set(self._initval)

        # Remove
        del self._initval

    def get(self):
        # Allocate an empty buffer
        buf = np.empty((self.nrow, self.ncol), dtype=self.dtype)

        # Copy
        memcpy2d_dtoh(buf, self.data, self.pitch, self.ncol*self.itemsize,
                      self.ncol*self.itemsize, self.nrow)

        # Reshape from a matrix to the expected I/O shape
        return buf.reshape(self.ioshape)

    def set(self, ary):
        if ary.shape != self.ioshape:
            raise ValueError('Invalid matrix shape')

        # Cast and compact from the I/O shape to a matrix
        nary = np.asanyarray(ary, dtype=self.dtype, order='C')
        nary = self.backend.compact_arr(nary)

        # Copy
        memcpy2d_htod(self.data, nary, self.ncol*self.itemsize, self.pitch,
                      self.ncol*self.itemsize, self.nrow)

    @property
    def _as_parameter_(self):
        return self.data

    def __long__(self):
        return self.data


class CUDAMatrix(CUDAMatrixBase, base.Matrix):
    def __init__(self, backend, ioshape, initval, extent, tags):
        super(CUDAMatrix, self).__init__(backend, backend.fpdtype, ioshape,
                                         initval, extent, tags)


class CUDAMatrixRSlice(base.MatrixRSlice):
    def __init__(self, backend, mat, p, q):
        super(CUDAMatrixRSlice, self).__init__(backend, mat, p, q)

        # Starting offset of our row
        self._soffset = p*mat.pitch

    @property
    def _as_parameter_(self):
        return self.parent.data + self._soffset

    @property
    def __long__(self):
        return self.parent.data + self._soffset


class CUDAMatrixBank(base.MatrixBank):
    def __long__(self):
        return self._curr_mat.data


class CUDAConstMatrix(CUDAMatrixBase, base.ConstMatrix):
    def __init__(self, backend, initval, extent, tags):
        ioshape = initval.shape
        super(CUDAConstMatrix, self).__init__(backend, backend.fpdtype,
                                              ioshape, initval, extent, tags)

class CUDAView(base.View):
    def __init__(self, backend, matmap, rcmap, stridemap, vlen, tags):
        super(CUDAView, self).__init__(backend, matmap, rcmap, stridemap,
                                       vlen, tags)

        # Row/column indcies of each view element
        r, c = rcmap[...,0], rcmap[...,1]

        # Go from matrices + row/column indcies to offsets relative to
        # the base allocation address
        offmap = np.array(c, dtype=np.int32)
        for m in self._mats:
            ix = np.where(matmap == m)
            offmap[ix] += m.offset + r[ix]*m.leaddim

        shape = (self.nrow, self.ncol)
        self.mapping = CUDAMatrixBase(backend, np.int32, shape, offmap,
                                      extent=None, tags=tags)
        self.strides = CUDAMatrixBase(backend, np.int32, shape, stridemap,
                                      extent=None, tags=tags)


class CUDAMPIMatrix(CUDAMatrix, base.MPIMatrix):
    def __init__(self, backend, ioshape, initval, extent, tags):
        # Call the standard matrix constructor
        super(CUDAMPIMatrix, self).__init__(backend, ioshape, initval, extent,
                                            tags)

        # Allocate a page-locked buffer on the host for MPI to send/recv from
        self.hdata = cuda.pagelocked_empty((self.nrow, self.ncol),
                                           self.dtype, 'C')


class CUDAMPIView(base.MPIView):
    def __init__(self, backend, matmap, rcmap, stridemap, vlen, tags):
        super(CUDAMPIView, self).__init__(backend, matmap, rcmap, stridemap,
                                          vlen, tags)


class CUDAQueue(base.Queue):
    def __init__(self):
        # Last kernel we executed
        self._last = None

        # CUDA stream and MPI request list
        self._stream_comp = cuda.Stream()
        self._stream_copy = cuda.Stream()
        self._mpireqs = []

        # Items waiting to be executed
        self._items = collections.deque()

    def __lshift__(self, items):
        self._items.extend(items)

    def __mod__(self, items):
        self.run()
        self << items
        self.run()

    def __nonzero__(self):
        return bool(self._items)

    def _exec_item(self, item, rtargs):
        if base.iscomputekernel(item):
            item.run(self._stream_comp, self._stream_copy, *rtargs)
        elif base.ismpikernel(item):
            item.run(self._mpireqs, *rtargs)
        else:
            raise ValueError('Non compute/MPI kernel in queue')
        self._last = item

    def _exec_next(self):
        item, rtargs = self._items.popleft()

        # If we are at a sequence point then wait for current items
        if self._at_sequence_point(item):
            self._wait()

        # Execute the item
        self._exec_item(item, rtargs)

    def _exec_nowait(self):
        while self._items and not self._at_sequence_point(self._items[0][0]):
            self._exec_item(*self._items.popleft())

    def _wait(self):
        if base.iscomputekernel(self._last):
            self._stream_comp.synchronize()
            self._stream_copy.synchronize()
        elif base.ismpikernel(self._last):
            MPI.Prequest.Waitall(self._mpireqs)
            self._mpireqs = []
        self._last = None

    def _at_sequence_point(self, item):
        iscompute, ismpi = base.iscomputekernel, base.ismpikernel

        if (iscompute(self._last) and not iscompute(item)) or\
           (ismpi(self._last) and not ismpi(item)):
            return True
        else:
            return False

    def run(self):
        while self._items:
            self._exec_next()
        self._wait()

    @staticmethod
    def runall(queues):
        # First run any items which will not result in an implicit wait
        for q in queues:
            q._exec_nowait()

        # So long as there are items remaining in the queues
        while any(queues):
            # Execute a (potentially) blocking item from each queue
            for q in it.ifilter(None, queues):
                q._exec_next()
                q._exec_nowait()

        # Wait for all tasks to complete
        for q in queues:
            q._wait()
