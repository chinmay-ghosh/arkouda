from __future__ import annotations
from typing import cast, Sequence, Union
from typeguard import typechecked
import json, struct
import numpy as np # type: ignore
from arkouda.client import generic_msg
from arkouda.dtypes import dtype, DTypes, resolve_scalar_dtype, structDtypeCodes, \
     translate_np_dtype, NUMBER_FORMAT_STRINGS
from arkouda.dtypes import int64 as akint64
from arkouda.dtypes import str_ as akstr_
from arkouda.dtypes import bool as akbool
from arkouda.logger import getArkoudaLogger
import builtins

__all__ = ["pdarray", "info", "clear", "any", "all", "is_sorted", "sum", "prod", 
           "min", "max", "argmin", "argmax", "mean", "var", "std", "mink", 
           "maxk", "argmink", "argmaxk", "register_pdarray", "attach_pdarray", 
           "unregister_pdarray"]

logger = getArkoudaLogger(name='pdarrayclass')    

@typechecked
def parse_single_value(msg : str) -> object:
    """
    Attempt to convert a scalar return value from the arkouda server to a
    numpy scalar in Python. The user should not call this function directly. 
    
    Parameters
    ----------
    msg : str
        scalar value in string form to be converted to a numpy scalar

    Returns
    -------
    object numpy scalar         
    """
    def unescape(s):
        escaping = False
        res = ''
        for c in s:
            if escaping:
                res += c
                escaping = False
            elif c == '\\':
                escaping = True
            else:
                res += c
        return res
    dtname, value = msg.split(maxsplit=1)
    mydtype = dtype(dtname)
    if mydtype == akbool:
        if value == "True":
            return mydtype.type(True)
        elif value == "False":
            return mydtype.type(False)
        else:
            raise ValueError(("unsupported value from server {} {}".\
                              format(mydtype.name, value)))
    try:
        if mydtype == akstr_:
            # String value will always be surrounded with double quotes, so remove them
            return mydtype.type(unescape(value[1:-1]))
        return mydtype.type(value)
    except:
        raise ValueError(("unsupported value from server {} {}".\
                              format(mydtype.name, value)))

# class for the pdarray
class pdarray:
    """
    The basic arkouda array class. This class contains only the
    attributies of the array; the data resides on the arkouda
    server. When a server operation results in a new array, arkouda
    will create a pdarray instance that points to the array data on
    the server. As such, the user should not initialize pdarray
    instances directly.

    Attributes
    ----------
    name : str
        The server-side identifier for the array
    dtype : dtype
        The element type of the array
    size : Union[int,np.int64]
        The number of elements in the array
    ndim : Union[int,np.int64]
        The rank of the array (currently only rank 1 arrays supported)
    shape : Sequence[int]
        A list or tuple containing the sizes of each dimension of the array
    itemsize : Union[int,np.int64]
        The size in bytes of each element
    """

    BinOps = frozenset(["+", "-", "*", "/", "//", "%", "<", ">", "<=", ">=", 
                        "!=", "==", "&", "|", "^", "<<", ">>","**"])
    OpEqOps = frozenset(["+=", "-=", "*=", "/=", "//=", "&=", "|=", "^=", 
                         "<<=", ">>=","**="])
    objtype = "pdarray"

    __array_priority__ = 1000

    def __init__(self, name : str, mydtype : np.dtype, size : Union[int,np.int64], 
                 ndim : Union[int,np.int64], shape: Sequence[int], 
                 itemsize : Union[int,np.int64]) -> None:
        self.name = name
        self.dtype = dtype(mydtype)
        self.size = size
        self.ndim = ndim
        self.shape = shape
        self.itemsize = itemsize

    def __del__(self):
        try:
            logger.debug('deleting pdarray with name {}'.format(self.name))
            generic_msg(cmd='delete', args='{}'.format(self.name))
        except:
            pass

    def __bool__(self) -> builtins.bool:
        if self.size != 1:
            raise ValueError(('The truth value of an array with more than one ' +
                              'element is ambiguous. Use a.any() or a.all()'))
        return builtins.bool(self[0])

    def __len__(self):
        return self.shape[0]

    def __str__(self):
        from arkouda.client import pdarrayIterThresh
        return generic_msg(cmd='str', args='{} {}'.format(self.name,pdarrayIterThresh))

    def __repr__(self):
        from arkouda.client import pdarrayIterThresh
        return generic_msg(cmd='repr',args='{} {}'.format(self.name,pdarrayIterThresh))

    def format_other(self, other : object) -> np.dtype:
        """
        Attempt to cast scalar other to the element dtype of this pdarray,
        and print the resulting value to a string (e.g. for sending to a
        server command). The user should not call this function directly.
        
        Parameters
        ----------
        other : object
            The scalar to be cast to the pdarray.dtype
            
        Returns
        -------
        np.dtype corresponding to the other parameter
        
        Raises
        ------
        TypeError
            Raised if the other parameter cannot be converted to
            Numpy dtype
        
        """
        try:
            other = self.dtype.type(other)
        except:
            raise TypeError("Unable to convert {} to {}".format(other, 
                                                                self.dtype.name))
        if self.dtype == bool:
            return str(other)
        fmt = NUMBER_FORMAT_STRINGS[self.dtype.name]
        return fmt.format(other)

    # binary operators
    def _binop(self, other : pdarray, op : str) -> pdarray:
        """
        Executes binary operation specified by the op string
        
        Parameters
        ----------
        other : pdarray
            The pdarray upon which the binop is to be executed
        op : str
            The binop to be executed
        
        Returns
        -------
        pdarray
            A pdarray encapsulating the binop result
            
        Raises
        ------
        ValueError
            Raised if the op is not within the pdarray.BinOps set, or if the
            pdarray sizes don't match
        TypeError
            Raised if other is not a pdarray or the pdarray.dtype is not
            a supported dtype
        
        """
        # For pdarray subclasses like ak.Datetime and ak.Timedelta, defer to child logic
        if type(other) != pdarray and issubclass(type(other), pdarray):
            return NotImplemented
        if op not in self.BinOps:
            raise ValueError("bad operator {}".format(op))
        # pdarray binop pdarray
        if isinstance(other, pdarray):
            if self.size != other.size:
                raise ValueError("size mismatch {} {}".format(self.size,other.size))
            cmd = "binopvv"
            args= "{} {} {}".format(op, self.name, other.name)
            repMsg = generic_msg(cmd=cmd,args=args)
            return create_pdarray(repMsg)
        # pdarray binop scalar
        dt = resolve_scalar_dtype(other)
        if dt not in DTypes:
            raise TypeError("Unhandled scalar type: {} ({})".format(other, 
                                                                    type(other)))
        cmd = "binopvs"
        args = "{} {} {} {}".\
                  format(op, self.name, dt, NUMBER_FORMAT_STRINGS[dt].format(other))
        repMsg = generic_msg(cmd=cmd,args=args)
        return create_pdarray(repMsg)

    # reverse binary operators
    # pdarray binop pdarray: taken care of by binop function
    def _r_binop(self, other : pdarray, op : str) -> pdarray:
        """
        Executes reverse binary operation specified by the op string
        
        Parameters
        ----------
        other : pdarray
            The pdarray upon which the reverse binop is to be executed
        op : str
            The name of the reverse binop to be executed
        
        Returns
        -------
        pdarray
            A pdarray encapsulating the reverse binop result
            
        Raises
        ------
        ValueError
            Raised if the op is not within the pdarray.BinOps set
        TypeError
            Raised if other is not a pdarray or the pdarray.dtype is not
            a supported dtype        
        """

        if op not in self.BinOps:
            raise ValueError("bad operator {}".format(op))
        # pdarray binop scalar
        dt = resolve_scalar_dtype(other)
        if dt not in DTypes:
            raise TypeError("Unhandled scalar type: {} ({})".format(other, 
                                                                    type(other)))
        cmd = "binopsv"
        args = "{} {} {} {}".\
                      format(op, dt, NUMBER_FORMAT_STRINGS[dt].format(other), 
                                                                    self.name)
        repMsg = generic_msg(cmd=cmd,args=args)
        return create_pdarray(repMsg)

    # overload + for pdarray, other can be {pdarray, int, float}
    def __add__(self, other):
        return self._binop(other, "+")

    def __radd__(self, other):
        return self._r_binop(other, "+")

    # overload - for pdarray, other can be {pdarray, int, float}
    def __sub__(self, other):
        return self._binop(other, "-")

    def __rsub__(self, other):
        return self._r_binop(other, "-")

    # overload * for pdarray, other can be {pdarray, int, float}
    def __mul__(self, other):
        return self._binop(other, "*")

    def __rmul__(self, other):
        return self._r_binop(other, "*")

    # overload / for pdarray, other can be {pdarray, int, float}
    def __truediv__(self, other):
        return self._binop(other, "/")

    def __rtruediv__(self, other):
        return self._r_binop(other, "/")

    # overload // for pdarray, other can be {pdarray, int, float}
    def __floordiv__(self, other):
        return self._binop(other, "//")

    def __rfloordiv__(self, other):
        return self._r_binop(other, "//")

    def __mod__(self, other):
        return self._binop(other, "%")

    def __rmod__(self, other):
        return self._r_binop(other, "%")

    # overload << for pdarray, other can be {pdarray, int}
    def __lshift__(self, other):
        return self._binop(other, "<<")

    def __rlshift__(self, other):
        return self._r_binop(other, "<<")

    # overload >> for pdarray, other can be {pdarray, int}
    def __rshift__(self, other):
        return self._binop(other, ">>")

    def __rrshift__(self, other):
        return self._r_binop(other, ">>")

    # overload & for pdarray, other can be {pdarray, int}
    def __and__(self, other):
        return self._binop(other, "&")

    def __rand__(self, other):
        return self._r_binop(other, "&")

    # overload | for pdarray, other can be {pdarray, int}
    def __or__(self, other):
        return self._binop(other, "|")

    def __ror__(self, other):
        return self._r_binop(other, "|")

    # overload | for pdarray, other can be {pdarray, int}
    def __xor__(self, other):
        return self._binop(other, "^")

    def __rxor__(self, other):
        return self._r_binop(other, "^")

    def __pow__(self,other):
        return self._binop(other,"**")

    def __rpow__(self,other):
        return self._r_binop(other,"**")

    # overloaded comparison operators
    def __lt__(self, other):
        return self._binop(other, "<")

    def __gt__(self, other):
        return self._binop(other, ">")

    def __le__(self, other):
        return self._binop(other, "<=")

    def __ge__(self, other):
        return self._binop(other, ">=")

    def __eq__(self, other):
        if (self.dtype == bool) and (isinstance(other, pdarray) and (other.dtype == bool)):
            return ~(self ^ other)
        else:
            return self._binop(other, "==")

    def __ne__(self, other):
        if (self.dtype == bool) and (isinstance(other, pdarray) and (other.dtype == bool)):
            return (self ^ other)
        else:
            return self._binop(other, "!=")

    # overload unary- for pdarray implemented as pdarray*(-1)
    def __neg__(self):
        return self._binop(-1, "*")

    # overload unary~ for pdarray implemented as pdarray^(~0)
    def __invert__(self):
        if self.dtype == akint64:
            return self._binop(~0, "^")
        if self.dtype == bool:
            return self._binop(True, "^")
        raise TypeError("Unhandled dtype: {} ({})".format(self, self.dtype))

    # op= operators
    def opeq(self, other, op):
        if op not in self.OpEqOps:
            raise ValueError("bad operator {}".format(op))
        # pdarray op= pdarray
        if isinstance(other, pdarray):
            if self.size != other.size:
                raise ValueError("size mismatch {} {}".format(self.size,other.size))
            generic_msg(cmd="opeqvv", args="{} {} {}".format(op, self.name, other.name))
            return self
        # pdarray binop scalar
        # opeq requires scalar to be cast as pdarray dtype
        try:
            other = self.dtype.type(other)
        except: # Can't cast other as dtype of pdarray
            raise TypeError("Unhandled scalar type: {} ({})".format(other, type(other)))

        cmd = "opeqvs"
        args = "{} {} {} {}".\
                         format(op, self.name, self.dtype.name, self.format_other(other))
        generic_msg(cmd=cmd,args=args)
        return self

    # overload += pdarray, other can be {pdarray, int, float}
    def __iadd__(self, other):
        return self.opeq(other, "+=")

    # overload -= pdarray, other can be {pdarray, int, float}
    def __isub__(self, other):
        return self.opeq(other, "-=")

    # overload *= pdarray, other can be {pdarray, int, float}
    def __imul__(self, other):
        return self.opeq(other, "*=")

    # overload /= pdarray, other can be {pdarray, int, float}
    def __itruediv__(self, other):
        return self.opeq(other, "/=")

    # overload //= pdarray, other can be {pdarray, int, float}
    def __ifloordiv__(self, other):
        return self.opeq(other, "//=")

    # overload <<= pdarray, other can be {pdarray, int, float}
    def __ilshift__(self, other):
        return self.opeq(other, "<<=")

    # overload >>= pdarray, other can be {pdarray, int, float}
    def __irshift__(self, other):
        return self.opeq(other, ">>=")

    # overload &= pdarray, other can be {pdarray, int, float}
    def __iand__(self, other):
        return self.opeq(other, "&=")

    # overload |= pdarray, other can be {pdarray, int, float}
    def __ior__(self, other):
        return self.opeq(other, "|=")

    # overload ^= pdarray, other can be {pdarray, int, float}
    def __ixor__(self, other):
        return self.opeq(other, "^=")
    def __ipow__(self, other):
        return self.opeq(other,"**=")
    
    def __iter__(self):
        raise NotImplementedError('pdarray does not support iteration')

    # overload a[] to treat like list
    def __getitem__(self, key):
        if np.isscalar(key) and resolve_scalar_dtype(key) == 'int64':
            orig_key = key
            if key < 0:
                # Interpret negative key as offset from end of array
                key += self.size
            if (key >= 0 and key < self.size):
                repMsg = generic_msg(cmd="[int]", args="{} {}".format(self.name, key))
                fields = repMsg.split()
                # value = fields[2]
                return parse_single_value(' '.join(fields[1:]))
            else:
                raise IndexError("[int] {} is out of bounds with size {}".format(orig_key,self.size))
        if isinstance(key, slice):
            (start,stop,stride) = key.indices(self.size)
            logger.debug('start: {} stop: {} stride: {}'.format(start,stop,stride))
            repMsg = generic_msg(cmd="[slice]", args="{} {} {} {}".format(self.name, start, stop, stride))
            return create_pdarray(repMsg);
        if isinstance(key, pdarray):
            kind, _ = translate_np_dtype(key.dtype)
            if kind not in ("bool", "int"):
                raise TypeError("unsupported pdarray index type {}".format(key.dtype))
            if kind == "bool" and self.size != key.size:
                raise ValueError("size mismatch {} {}".format(self.size,key.size))
            repMsg = generic_msg(cmd="[pdarray]", args="{} {}".format(self.name, key.name))
            return create_pdarray(repMsg)
        else:
            raise TypeError("Unhandled key type: {} ({})".format(key, type(key)))

    def __setitem__(self, key, value):
        if np.isscalar(key) and resolve_scalar_dtype(key) == 'int64':
            orig_key = key
            if key < 0:
                # Interpret negative key as offset from end of array
                key += self.size
            if (key >= 0 and key < self.size):
                generic_msg(cmd="[int]=val", args="{} {} {} {}".\
                            format(self.name, key, self.dtype.name, 
                                   self.format_other(value)))
            else:
                raise IndexError(("index {} is out of bounds with size {}".\
                                 format(orig_key,self.size)))
        elif isinstance(key, pdarray):
            if isinstance(value, pdarray):
                generic_msg(cmd="[pdarray]=pdarray", args="{} {} {}".\
                            format(self.name,key.name,value.name))
            else:
                generic_msg(cmd="[pdarray]=val", args="{} {} {} {}".\
                            format(self.name, key.name, self.dtype.name, 
                                   self.format_other(value)))
        elif isinstance(key, slice):
            (start,stop,stride) = key.indices(self.size)
            logger.debug('start: {} stop: {} stride: {}'.format(start,stop,stride))
            if isinstance(value, pdarray):
                generic_msg(cmd="[slice]=pdarray", args="{} {} {} {} {}".\
                            format(self.name,start,stop,stride,value.name))
            else:
                generic_msg(cmd="[slice]=val", args="{} {} {} {} {} {}".\
                            format(self.name, start, stop, stride, self.dtype.name, 
                                   self.format_other(value)))
        else:
            raise TypeError("Unhandled key type: {} ({})".\
                            format(key, type(key)))

    @typechecked
    def fill(self, value : Union[int,np.int64,float,np.float64]) -> None:
        """
        Fill the array (in place) with a constant value.
        
        Parameters
        ----------
        value : Union[int,np.int64,float,np.float64]
        
        Raises
        -------
        TypeError
            Raised if value is not an int, int64, float, or float64         
        """
        generic_msg(cmd="set", args="{} {} {}".format(self.name, 
                                        self.dtype.name, self.format_other(value)))

    def any(self) -> np.bool_:
        """
        Return True iff any element of the array evaluates to True.
        """
        return any(self)

    def all(self) -> np.bool_:
        """
        Return True iff all elements of the array evaluate to True.
        """
        return all(self)

    def is_sorted(self) -> np.bool_:
        """
        Return True iff the array is monotonically non-decreasing.
        
        Parameters
        ----------
        None
        
        Returns
        -------
        bool 
            Indicates if the array is monotonically non-decreasing
            
        Raises
        ------
        TypeError
            Raised if pda is not a pdarray instance
        RuntimeError
            Raised if there's a server-side error thrown
        """
        return is_sorted(self)

    def sum(self) -> Union[np.float64,np.int64]:
        """
        Return the sum of all elements in the array.
        """
        return sum(self)

    def prod(self) -> np.float64:
        """
        Return the product of all elements in the array. Return value is
        always a np.float64 or np.int64.
        """
        return prod(self)

    def min(self) -> Union[np.float64,np.int64]:
        """
        Return the minimum value of the array.
        """
        return min(self)

    def max(self) -> Union[np.float64,np.int64]:
        """
        Return the maximum value of the array.
        """
        return max(self)

    def argmin(self) -> np.int64:
        """
        Return the index of the first occurrence of the array min value
        """
        return argmin(self)

    def argmax(self) -> np.int64:
        """
        Return the index of the first occurrence of the array max value.
        """
        return argmax(self)

    def mean(self) -> np.float64:
        """
        Return the mean of the array.
        """
        return mean(self)

    def var(self, ddof : Union[int,np.int64]=0) -> np.float64:
        """
        Compute the variance. See ``arkouda.var`` for details.
        
        Parameters
        ----------
        ddof : Union[int,np.int64]
            "Delta Degrees of Freedom" used in calculating var

        Returns
        -------
        np.float64
            The scalar variance of the array

        Raises
        ------
        TypeError
            Raised if pda is not a pdarray instance
        ValueError
            Raised if the ddof >= pdarray size
        RuntimeError
            Raised if there's a server-side error thrown

        """
        return var(self, ddof=ddof)

    def std(self, ddof : Union[int,np.int64]=0) -> np.float64:
        """
        Compute the standard deviation. See ``arkouda.std`` for details.
        
        Parameters
        ----------
        ddof : Union[int,np.int64]
            "Delta Degrees of Freedom" used in calculating std

        Returns
        -------
        np.float64
            The scalar standard deviation of the array

        Raises
        ------
        TypeError
            Raised if pda is not a pdarray instance
        RuntimeError
            Raised if there's a server-side error thrown
        """
        return std(self, ddof=ddof)

    def mink(self, k : Union[int,np.int64]) -> pdarray:
        """
        Compute the minimum "k" values.
        
        Parameters
        ----------
        k : Union[int,np.int64]
            The desired count of maximum values to be returned by the output.

        Returns
        -------
        pdarray, int
            The maximum `k` values from pda
        
        Raises
        ------
        TypeError
            Raised if pda is not a pdarray
        """
        return mink(self,k)

    @typechecked
    def maxk(self, k : Union[int,np.int64]) -> pdarray:
        """
        Compute the maximum "k" values.
        
        Parameters
        ----------
        k : Union[int,np.int64]
            The desired count of maximum values to be returned by the output.

        Returns
        -------
        pdarray, int
            The maximum `k` values from pda
        
        Raises
        ------
        TypeError
            Raised if pda is not a pdarray
        """
        return maxk(self,k)

    def argmink(self, k : Union[int,np.int64]) -> pdarray:
        """
        Compute the minimum "k" values.
        
        Parameters
        ----------
        k : Union[int,np.int64]
            The desired count of maximum values to be returned by the output.

        Returns
        -------
        pdarray, int
            Indices corresponding to the maximum `k` values from pda
        
        Raises
        ------
        TypeError
            Raised if pda is not a pdarray
        """
        return argmink(self,k)

    def argmaxk(self, k : Union[int,np.int64]) -> pdarray:
        """
        Finds the indices corresponding to the maximum "k" values.
        
        Parameters
        ----------
        k : Union[int,np.int64]
            The desired count of maximum values to be returned by the output.

        Returns
        -------
        pdarray, int
            Indices corresponding to the  maximum `k` values, sorted
        
        Raises
        ------
        TypeError
            Raised if pda is not a pdarray
        """
        return argmaxk(self,k)

    
    def to_ndarray(self) -> np.ndarray:
        """
        Convert the array to a np.ndarray, transferring array data from the
        Arkouda server to client-side Python. Note: if the pdarray size exceeds 
        client.maxTransferBytes, a RuntimeError is raised.

        Returns
        -------
        np.ndarray
            A numpy ndarray with the same attributes and data as the pdarray

        Raises
        ------
        RuntimeError
            Raised if there is a server-side error thrown, if the pdarray size
            exceeds the built-in client.maxTransferBytes size limit, or if the bytes
            received does not match expected number of bytes
        Notes
        -----
        The number of bytes in the array cannot exceed ``client.maxTransferBytes``,
        otherwise a ``RuntimeError`` will be raised. This is to protect the user
        from overflowing the memory of the system on which the Python client
        is running, under the assumption that the server is running on a
        distributed system with much more memory than the client. The user
        may override this limit by setting client.maxTransferBytes to a larger
        value, but proceed with caution.

        See Also
        --------
        array

        Examples
        --------
        >>> a = ak.arange(0, 5, 1)
        >>> a.to_ndarray()
        array([0, 1, 2, 3, 4])

        >>> type(a.to_ndarray())
        numpy.ndarray
        """
        from arkouda.client import maxTransferBytes
        # Total number of bytes in the array data
        arraybytes = self.size * self.dtype.itemsize
        # Guard against overflowing client memory
        if arraybytes > maxTransferBytes:
            raise RuntimeError(('Array exceeds allowed size for transfer. Increase ' +
                               'client.maxTransferBytes to allow'))
        # The reply from the server will be a bytes object
        rep_msg = generic_msg(cmd="tondarray", args="{}".format(self.name), recv_bytes=True)
        # Make sure the received data has the expected length
        if len(rep_msg) != self.size*self.dtype.itemsize:
            raise RuntimeError("Expected {} bytes but received {}".\
                               format(self.size*self.dtype.itemsize, len(rep_msg)))
        # Use struct to interpret bytes as a big-endian numeric array
        fmt = '>{:n}{}'.format(self.size, structDtypeCodes[self.dtype.name])
        # Return a numpy ndarray
        return np.array(struct.unpack(fmt, rep_msg)) # type: ignore

    def to_cuda(self):
        """
        Convert the array to a Numba DeviceND array, transferring array data from the
        arkouda server to Python via ndarray. If the array exceeds a builtin size limit,
        a RuntimeError is raised.

        Returns
        -------
        numba.DeviceNDArray
            A Numba ndarray with the same attributes and data as the pdarray; on GPU

        Raises
        ------
        ImportError
            Raised if CUDA is not available
        ModuleNotFoundError
            Raised if Numba is either not installed or not enabled
        RuntimeError
            Raised if there is a server-side error thrown in the course of retrieving
            the pdarray.

        Notes
        -----
        The number of bytes in the array cannot exceed ``client.maxTransferBytes``,
        otherwise a ``RuntimeError`` will be raised. This is to protect the user
        from overflowing the memory of the system on which the Python client
        is running, under the assumption that the server is running on a
        distributed system with much more memory than the client. The user
        may override this limit by setting client.maxTransferBytes to a larger
        value, but proceed with caution.

        See Also
        --------
        array

        Examples
        --------
        >>> a = ak.arange(0, 5, 1)
        >>> a.to_cuda()
        array([0, 1, 2, 3, 4])

        >>> type(a.to_cuda())
        numpy.devicendarray
        """
        try:
            from numba import cuda # type: ignore
            if not(cuda.is_available()):
                raise ImportError(('CUDA is not available. Check for the CUDA toolkit ' +
                                  'and ensure a GPU is installed.'))
        except:
            raise ModuleNotFoundError(('Numba is not enabled or installed and ' +
                                      'is required for GPU support.'))

        # Total number of bytes in the array data
        arraybytes = self.size * self.dtype.itemsize
        
        from arkouda.client import maxTransferBytes
        # Guard against overflowing client memory
        if arraybytes > maxTransferBytes:
            raise RuntimeError(("Array exceeds allowed size for transfer. " +
                               "Increase client.maxTransferBytes to allow"))
        # The reply from the server will be a bytes object
        rep_msg = generic_msg(cmd="tondarray", args="{}".format(self.name), recv_bytes=True)
        # Make sure the received data has the expected length
        if len(rep_msg) != self.size*self.dtype.itemsize:
            raise RuntimeError("Expected {} bytes but received {}".\
                               format(self.size*self.dtype.itemsize, len(rep_msg)))
        # Use struct to interpret bytes as a big-endian numeric array
        fmt = '>{:n}{}'.format(self.size, structDtypeCodes[self.dtype.name])
        # Return a numba devicendarray
        return cuda.to_device(struct.unpack(fmt, rep_msg))

    @typechecked
    def save(self, prefix_path : str, dataset : str='array', mode : str='truncate') -> str:
        """
        Save the pdarray to HDF5. The result is a collection of HDF5 files,
        one file per locale of the arkouda server, where each filename starts
        with prefix_path. Each locale saves its chunk of the array to its
        corresponding file.

        Parameters
        ----------
        prefix_path : str
            Directory and filename prefix that all output files share
        dataset : str
            Name of the dataset to create in HDF5 files (must not already exist)
        mode : str {'truncate' | 'append'}
            By default, truncate (overwrite) output files, if they exist.
            If 'append', attempt to create new dataset in existing files.

        Returns
        -------
        string message indicating result of save operation

        Raises
        ------
        RuntimeError
            Raised if a server-side error is thrown saving the pdarray
        ValueError
            Raised if there is an error in parsing the prefix path pointing to
            file write location or if the mode parameter is neither truncate
            nor append
        TypeError
            Raised if any one of the prefix_path, dataset, or mode parameters
            is not a string

        See Also
        --------
        save_all, load, read_hdf, read_all

        Notes
        -----
        The prefix_path must be visible to the arkouda server and the user must
        have write permission.

        Output files have names of the form ``<prefix_path>_LOCALE<i>.hdf``, where ``<i>``
        ranges from 0 to ``numLocales``. If any of the output files already exist and
        the mode is 'truncate', they will be overwritten. If the mode is 'append'
        and the number of output files is less than the number of locales or a
        dataset with the same name already exists, a ``RuntimeError`` will result.

        Examples
        --------
        >>> a = ak.arange(0, 100, 1)
        >>> a.save('arkouda_range', dataset='array')

        Array is saved in numLocales files with names like ``tmp/arkouda_range_LOCALE0.hdf``

        The array can be read back in as follows

        >>> b = ak.load('arkouda_range', dataset='array')
        >>> (a == b).all()
        True
        """
        if mode.lower() in 'append':
            m = 1
        elif mode.lower() in 'truncate':
            m = 0
        else:
            raise ValueError("Allowed modes are 'truncate' and 'append'")

        """
        If offsets are provided, add to the json_array as the offsets will be used to 
        retrieve the array elements from the hdf5 files.
        """ 
        try:
            json_array = json.dumps([prefix_path])
        except Exception as e:
            raise ValueError(e)
        return cast(str, generic_msg(cmd="tohdf", args="{} {} {} {} {}".\
                           format(self.name, dataset, m, json_array, self.dtype)))


    def register(self, user_defined_name : str) -> pdarray:
        """
        Return a pdarray with a user defined name in the arkouda server 
        so it can be attached to later using pdarray.attach()
        
        Parameters
        ----------
        user_defined_name : str
            user defined name array is to be registered under
        
        Returns
        -------
        pdarray
            pdarray which points to original input pdarray but is also 
            registered with user defined name in the arkouda server
        
        Raises
        ------
        TypeError
            Raised if pda is neither a pdarray nor a str or if 
            user_defined_name is not a str
        
        See also
        --------
        attach, unregister
        
        Notes
        -----
        Registered names/pdarrays in the server are immune to deletion 
        until they are unregistered.
        
        Examples
        --------
        >>> a = zeros(100)
        >>> r_pda = a.register("my_zeros")
        >>> # potentially disconnect from server and reconnect to server
        >>> b = ak.pdarray.attach("my_zeros")
        >>> # ...other work...
        >>> b.unregister()
        """
        return register_pdarray(self, user_defined_name)

    def unregister(self) -> None:
        """
        Unregister a pdarray in the arkouda server which was previously 
        registered using register() and/or attahced to using attach()
        
        Parameters
        ----------
        user_defined_name : str
            which array was registered under
        
        Returns
        -------
        None
        
        Raises 
        ------
        TypeError
            Raised if pda is neither a pdarray nor a str
        
        See also
        --------
        register, unregister
        
        Notes
        -----
        Registered names/pdarrays in the server are immune to deletion until 
        they are unregistered.
        
        Examples
        --------
        >>> a = zeros(100)
        >>> r_pda = a.register("my_zeros")
        >>> # potentially disconnect from server and reconnect to server
        >>> b = ak.pdarray.attach("my_zeros")
        >>> # ...other work...
        >>> b.unregister()
        """
        unregister_pdarray(self)
        
    # class method self is not passed in
    # invoke with ak.pdarray.attach('user_defined_name')
    @staticmethod
    def attach(user_defined_name : str) -> pdarray:
        """
        class method to return a pdarray attached to the a registered name in the arkouda 
        server which was registered using register()
        
        Parameters
        ----------
        user_defined_name : str
            user defined name which array was registered under
        
        Returns
        -------
        pdarray
            pdarray which points to pdarray registered with user defined
            name in the arkouda server
        
        Raises
        ------
        TypeError
            Raised if user_defined_name is not a str
        
        See also
        --------
        register, unregister
        
        Notes
        -----
        Registered names/pdarrays in the server are immune to deletion 
        until they are unregistered.
        
        Examples
        --------
        >>> a = zeros(100)
        >>> r_pda = a.register("my_zeros")
        >>> # potentially disconnect from server and reconnect to server
        >>> b = ak.pdarray.attach("my_zeros")
        >>> # ...other work...
        >>> b.unregister()
        """
        return attach_pdarray(user_defined_name)

#end pdarray class def
    
# creates pdarray object
#   only after:
#       all values have been checked by python module and...
#       server has created pdarray already before this is called
#       server has created pdarray already befroe this is called
@typechecked
def create_pdarray(repMsg : str) -> pdarray:
    """
    Return a pdarray instance pointing to an array created by the arkouda server.
    The user should not call this function directly.

    Parameters
    ----------
    repMsg : str
        space-delimited string containing the pdarray name, datatype, size
        dimension, shape,and itemsize

    Returns
    -------
    pdarray
        A pdarray with the same attributes and data as the pdarray; on GPU

    Raises
-   -----
    ValueError
        If there's an error in parsing the repMsg parameter into the six 
        values needed to create the pdarray instance
    RuntimeError
        Raised if a server-side error is thrown in the process of creating
        the pdarray instance
    """
    try:
        fields = repMsg.split()
        name = fields[1]
        mydtype = fields[2]
        size = int(fields[3])
        ndim = int(fields[4])
        shape = [int(el) for el in fields[5][1:-1].split(',')]
        itemsize = int(fields[6])
    except Exception as e:
        raise ValueError(e)
    logger.debug(("created Chapel array with name: {} dtype: {} size: {} ndim: {} shape: {} " +
                  "itemsize: {}").format(name, mydtype, size, ndim, shape, itemsize))
    return pdarray(name, mydtype, size, ndim, shape, itemsize)

@typechecked
def info(pda : Union[pdarray, str]) -> str:
    """
    Returns information about the pdarray instance
    
    Parameters
    ----------
    pda : Union[pdarray, str]
       pda is either the pdarray instance or the pdarray.name string
    
    Returns
    ------
    str
        Information regarding the pdarray in the form of a string
    
    Raises
    ------
    TypeError
        Raised if the parameter is neither a pdarray or string
    RuntimeError
        Raised if a server-side error is thrown in the process of 
        retrieving information about the pdarray
    """
    if isinstance(pda, pdarray):
        return cast(str,generic_msg(cmd="info", args="{}".format(pda.name)))
    elif isinstance(pda, str):
        return cast(str,generic_msg(cmd="info", args="{}".format(pda)))
    else:
        raise TypeError("info: must be pdarray or string".format(pda))
        return generic_msg(cmd="info", args="{}".format(pda))

def clear() -> None:
    """
    Send a clear message to clear all unregistered data from the server symbol table

    Returns
    -------
    None

    Raises
    ------  
    RuntimeError
        Raised if there is a server-side error in executing clear request
    """
    generic_msg(cmd="clear")

@typechecked
def any(pda : pdarray) -> np.bool_:
    """
    Return True iff any element of the array evaluates to True.
    
    Parameters
    ----------
    pda : pdarray
        The pdarray instance to be evaluated    
    
    Returns
    -------
    bool 
        Indicates if 1..n pdarray elements evaluate to True
        
    Raises
    ------
    TypeError
        Raised if pda is not a pdarray instance
    RuntimeError
        Raised if there's a server-side error thrown
    """
    repMsg = generic_msg(cmd="reduction", args="{} {}".format("any", pda.name))
    return parse_single_value(cast(str,repMsg))

@typechecked
def all(pda : pdarray) -> np.bool_:
    """
    Return True iff all elements of the array evaluate to True.

    Parameters
    ----------
    pda : pdarray
        The pdarray instance to be evaluated

    Returns
    -------
    bool 
        Indicates if all pdarray elements evaluate to True
        
    Raises
    ------
    TypeError
        Raised if pda is not a pdarray instance
    RuntimeError
        Raised if there's a server-side error thrown
    """
    repMsg = generic_msg(cmd="reduction", args="{} {}".format("all", pda.name))
    return parse_single_value(cast(str,repMsg))

@typechecked
def is_sorted(pda : pdarray) -> np.bool_:
    """
    Return True iff the array is monotonically non-decreasing.
    
    Parameters
    ----------
    pda : pdarray
        The pdarray instance to be evaluated
    
    Returns
    -------
    bool 
        Indicates if the array is monotonically non-decreasing
        
    Raises
    ------
    TypeError
        Raised if pda is not a pdarray instance
    RuntimeError
        Raised if there's a server-side error thrown
    """
    repMsg = generic_msg(cmd="reduction", args="{} {}".format("is_sorted", pda.name))
    return parse_single_value(cast(str,repMsg))

@typechecked
def sum(pda : pdarray) -> np.float64:
    """
    Return the sum of all elements in the array.

    Parameters
    ----------
    pda : pdarray
        Values for which to calculate the sum
    
    Returns
    -------
    np.float64
        The sum of all elements in the array
        
    Raises
    ------
    TypeError
        Raised if pda is not a pdarray instance
    RuntimeError
        Raised if there's a server-side error thrown
    """
    repMsg = generic_msg(cmd="reduction", args="{} {}".format("sum", pda.name))
    return parse_single_value(cast(str,repMsg))

@typechecked
def prod(pda : pdarray) -> np.float64:
    """
    Return the product of all elements in the array. Return value is
    always a np.float64 or np.int64
    
    Parameters
    ----------
    pda : pdarray
        Values for which to calculate the product

    Returns
    -------
    Union[np.float64,np.int64]
        The product calculated from the pda
        
    Raises
    ------
    TypeError
        Raised if pda is not a pdarray instance
    RuntimeError
        Raised if there's a server-side error thrown
    """
    repMsg = generic_msg(cmd="reduction", args="{} {}".format("prod", pda.name))
    
    return parse_single_value(cast(str,repMsg))

def min(pda : pdarray) -> Union[np.float64,np.int64]:
    """
    Return the minimum value of the array.
    
    Parameters
    ----------
    pda : pdarray
        Values for which to calculate the min

    Returns
    -------
    Union[np.float64,np.int64]
        The min calculated from the pda
        
    Raises
    ------
    TypeError
        Raised if pda is not a pdarray instance
    RuntimeError
        Raised if there's a server-side error thrown
    """
    repMsg = generic_msg(cmd="reduction",args="{} {}".format("min", pda.name))
    return parse_single_value(cast(str,repMsg))

@typechecked
def max(pda : pdarray) -> Union[np.float64,np.int64]:
    """
    Return the maximum value of the array.
    
    Parameters
    ----------
    pda : pdarray
        Values for which to calculate the max

    Returns
    -------
    Union[np.float64,np.int64]:
        The max calculated from the pda
       
    Raises
    ------
    TypeError
        Raised if pda is not a pdarray instance
    RuntimeError
        Raised if there's a server-side error thrown
    """
    repMsg = generic_msg(cmd="reduction", args="{} {}".format("max", pda.name))
    return parse_single_value(cast(str,repMsg))

@typechecked
def argmin(pda : pdarray) -> np.int64:
    """
    Return the index of the first occurrence of the array min value.

    Parameters
    ----------
    pda : pdarray
        Values for which to calculate the argmin

    Returns
    -------
    np.int64
        The index of the argmin calculated from the pda
        
    Raises
    ------
    TypeError
        Raised if pda is not a pdarray instance
    RuntimeError
        Raised if there's a server-side error thrown
    """
    repMsg = generic_msg(cmd="reduction", args="{} {}".format("argmin", pda.name))
    return parse_single_value(cast(str,repMsg))

@typechecked
def argmax(pda : pdarray) -> np.int64:
    """
    Return the index of the first occurrence of the array max value.
    
    Parameters
    ----------
    pda : pdarray
        Values for which to calculate the argmax

    Returns
    -------
    np.int64
        The index of the argmax calculated from the pda

    Raises
    ------
    TypeError
        Raised if pda is not a pdarray instance
    RuntimeError
        Raised if there's a server-side error thrown
    """
    repMsg = generic_msg(cmd="reduction", args="{} {}".format("argmax", pda.name))
    return parse_single_value(cast(str,repMsg))

@typechecked
def mean(pda : pdarray) -> np.float64:
    """
    Return the mean of the array.
    
    Parameters
    ----------
    pda : pdarray
        Values for which to calculate the mean

    Returns
    -------
    np.float64
        The mean calculated from the pda sum and size

    Raises
    ------
    TypeError
        Raised if pda is not a pdarray instance
    RuntimeError
        Raised if there's a server-side error thrown
    """
    return pda.sum() / pda.size

@typechecked
def var(pda : pdarray, ddof : Union[int,np.int64]=0) -> np.float64:
    """
    Return the variance of values in the array.

    Parameters
    ----------
    pda : pdarray
        Values for which to calculate the variance
    ddof : Union[int,np.int64]
        "Delta Degrees of Freedom" used in calculating var

    Returns
    -------
    np.float64
        The scalar variance of the array

    Raises
    ------
    TypeError
        Raised if pda is not a pdarray instance
    ValueError
        Raised if the ddof >= pdarray size
    RuntimeError
        Raised if there's a server-side error thrown

    See Also
    --------
    mean, std

    Notes
    -----
    The variance is the average of the squared deviations from the mean,
    i.e.,  ``var = mean((x - x.mean())**2)``.

    The mean is normally calculated as ``x.sum() / N``, where ``N = len(x)``.
    If, however, `ddof` is specified, the divisor ``N - ddof`` is used
    instead.  In standard statistical practice, ``ddof=1`` provides an
    unbiased estimator of the variance of a hypothetical infinite population.
    ``ddof=0`` provides a maximum likelihood estimate of the variance for
    normally distributed variables.
    """
    if ddof >= pda.size:
        raise ValueError("var: ddof must be less than number of values")
    m = mean(pda)
    return ((pda - m)**2).sum() / (pda.size - ddof)

@typechecked
def std(pda : pdarray, ddof : Union[int,np.int64]=0) -> np.float64:
    """
    Return the standard deviation of values in the array. The standard
    deviation is implemented as the square root of the variance.

    Parameters
    ----------
    pda : pdarray
        values for which to calculate the standard deviation
    ddof : Union[int,np.int64]
        "Delta Degrees of Freedom" used in calculating std

    Returns
    -------
    np.float64
        The scalar standard deviation of the array

    Raises
    ------
    TypeError
        Raised if pda is not a pdarray instance or ddof is not an integer
    ValueError
        Raised if ddof is an integer < 0
    RuntimeError
        Raised if there's a server-side error thrown

    See Also
    --------
    mean, var

    Notes
    -----
    The standard deviation is the square root of the average of the squared
    deviations from the mean, i.e., ``std = sqrt(mean((x - x.mean())**2))``.

    The average squared deviation is normally calculated as
    ``x.sum() / N``, where ``N = len(x)``.  If, however, `ddof` is specified,
    the divisor ``N - ddof`` is used instead. In standard statistical
    practice, ``ddof=1`` provides an unbiased estimator of the variance
    of the infinite population. ``ddof=0`` provides a maximum likelihood
    estimate of the variance for normally distributed variables. The
    standard deviation computed in this function is the square root of
    the estimated variance, so even with ``ddof=1``, it will not be an
    unbiased estimate of the standard deviation per se.
    """
    if ddof < 0:
        raise ValueError("ddof must be an integer 0 or greater")

    return np.sqrt(var(pda, ddof=ddof))

@typechecked
def mink(pda : pdarray, k : Union[int,np.int64]) -> pdarray:
    """
    Find the `k` minimum values of an array.

    Returns the smallest `k` values of an array, sorted

    Parameters
    ----------
    pda : pdarray
        Input array.
    k : Union[int,np.int64]
        The desired count of minimum values to be returned by the output.

    Returns
    -------
    pdarray
        The minimum `k` values from pda, sorted
        
    Raises
    ------
    TypeError
        Raised if pda is not a pdarray
    ValueError
        Raised if the pda is empty or k < 1

    Notes
    -----
    This call is equivalent in value to:
    
        a[ak.argsort(a)[:k]]
    
    and generally outperforms this operation.

    This reduction will see a significant drop in performance as `k` grows
    beyond a certain value. This value is system dependent, but generally
    about a `k` of 5 million is where performance degredation has been observed.
    
    Examples
    --------
    >>> A = ak.array([10,5,1,3,7,2,9,0])
    >>> ak.mink(A, 3)
    array([0, 1, 2])
    >>> ak.mink(A, 4)
    array([0, 1, 2, 3])
    """
    if k < 1:
        raise ValueError('k must be 1 or greater')
    if pda.size == 0:
        raise ValueError("must be a non-empty pdarray of type int or float")

    repMsg = generic_msg(cmd="mink", args="{} {} {}".format(pda.name, k, False))
    return create_pdarray(cast(str,repMsg))

@typechecked
def maxk(pda : pdarray, k : Union[int,np.int64]) -> pdarray:
    """
    Find the `k` maximum values of an array.

    Returns the largest `k` values of an array, sorted

    Parameters
    ----------
    pda : pdarray
        Input array.
    k : Union[int,np.int64]
        The desired count of maximum values to be returned by the output.

    Returns
    -------
    pdarray, int
        The maximum `k` values from pda, sorted
        
    Raises
    ------
    TypeError
        Raised if pda is not a pdarray or k is not an integer
    ValueError
        Raised if the pda is empty or k < 1

    Notes
    -----
    This call is equivalent in value to:
    
        a[ak.argsort(a)[k:]]
    
    and generally outperforms this operation.

    This reduction will see a significant drop in performance as `k` grows
    beyond a certain value. This value is system dependent, but generally
    about a `k` of 5 million is where performance degredation has been observed.


    Examples
    --------
    >>> A = ak.array([10,5,1,3,7,2,9,0])
    >>> ak.maxk(A, 3)
    array([7, 9, 10])
    >>> ak.maxk(A, 4)
    array([5, 7, 9, 10])
    """
    if k < 1:
        raise ValueError('k must be 1 or greater')
    if pda.size == 0:
        raise ValueError("must be a non-empty pdarray of type int or float")

    repMsg = generic_msg(cmd="maxk", args="{} {} {}".format(pda.name, k, False))
    return create_pdarray(repMsg)

@typechecked
def argmink(pda : pdarray, k : Union[int,np.int64]) -> pdarray:
    """
    Finds the indices corresponding to the `k` minimum values of an array.

    Parameters
    ----------
    pda : pdarray
        Input array.
    k : Union[int,np.int64]
        The desired count of indices corresponding to minimum array values

    Returns
    -------
    pdarray, int
        The indices of the minimum `k` values from the pda, sorted
        
    Raises
    ------
    TypeError
        Raised if pda is not a pdarray or k is not an integer
    ValueError
        Raised if the pda is empty or k < 1

    Notes
    -----
    This call is equivalent in value to:
    
        ak.argsort(a)[:k]
    
    and generally outperforms this operation.

    This reduction will see a significant drop in performance as `k` grows
    beyond a certain value. This value is system dependent, but generally
    about a `k` of 5 million is where performance degradation has been observed.

    Examples
    --------
    >>> A = ak.array([10,5,1,3,7,2,9,0])
    >>> ak.argmink(A, 3)
    array([7, 2, 5])
    >>> ak.argmink(A, 4)
    array([7, 2, 5, 3])
    """
    if k < 1:
        raise ValueError('k must be 1 or greater')
    if pda.size == 0:
        raise ValueError("must be a non-empty pdarray of type int or float")

    repMsg = generic_msg(cmd="mink", args="{} {} {}".format(pda.name, k, True))
    return create_pdarray(repMsg)

@typechecked
def argmaxk(pda : pdarray, k : Union[int,np.int64]) -> pdarray:
    """
    Find the indices corresponding to the `k` maximum values of an array.

    Returns the largest `k` values of an array, sorted

    Parameters
    ----------
    pda : pdarray
        Input array.
    k : Union[int,np.int64]
        The desired count of indices corresponding to maxmum array values

    Returns
    -------
    pdarray, int
        The indices of the maximum `k` values from the pda, sorted
        
    Raises
    ------   
    TypeError
        Raised if pda is not a pdarray or k is not an integer
    ValueError
        Raised if the pda is empty or k < 1

    Notes
    -----
    This call is equivalent in value to:
    
        ak.argsort(a)[k:]
    
    and generally outperforms this operation.

    This reduction will see a significant drop in performance as `k` grows
    beyond a certain value. This value is system dependent, but generally
    about a `k` of 5 million is where performance degradation has been observed.


    Examples
    --------
    >>> A = ak.array([10,5,1,3,7,2,9,0])
    >>> ak.argmaxk(A, 3)
    array([4, 6, 0])
    >>> ak.argmaxk(A, 4)
    array([1, 4, 6, 0])
    """
    if k < 1:
        raise ValueError('k must be 1 or greater')
    if pda.size == 0:
        raise ValueError("must be a non-empty pdarray of type int or float")

    repMsg = generic_msg(cmd="maxk", args="{} {} {}".format(pda.name, k, True))
    return create_pdarray(repMsg)


@typechecked
def register_pdarray(pda : Union[str,pdarray], user_defined_name : str) -> pdarray:
    """
    Return a pdarray with a user defined name in the arkouda server 
    so it can be attached to later using attach_pdarray()
    
    Parameters
    ----------
    pda : str or pdarray
        the array to register
    user_defined_name : str
        user defined name array is to be registered under

    Returns
    -------
    pdarray
        pdarray which points to original input pdarray but is also 
        registered with user defined name in the arkouda server


    Raises
    ------
    TypeError
        Raised if pda is neither a pdarray nor a str or if 
        user_defined_name is not a str

    See also
    --------
    attach_pdarray, unregister_pdarray

    Notes
    -----
    Registered names/pdarrays in the server are immune to deletion 
    until they are unregistered.

    Examples
    --------
    >>> a = zeros(100)
    >>> r_pda = ak.register_pda(a, "my_zeros")
    >>> # potentially disconnect from server and reconnect to server
    >>> b = ak.attach_pda("my_zeros")
    >>> # ...other work...
    >>> ak.unregister_pda(b)
    """

    if isinstance(pda, pdarray):
        repMsg = generic_msg(cmd="register", args="{} {}".\
                             format(pda.name, user_defined_name))
        return create_pdarray(repMsg)

    if isinstance(pda, str):
        repMsg = generic_msg(cmd="register", args="{} {}".\
                             format(pda, user_defined_name))        
        return create_pdarray(repMsg)


@typechecked
def attach_pdarray(user_defined_name : str) -> pdarray:
    """
    Return a pdarray attached to the a registered name in the arkouda 
    server which was registered using register_pdarray()
    
    Parameters
    ----------
    user_defined_name : str
        user defined name which array was registered under

    Returns
    -------
    pdarray
        pdarray which points to pdarray registered with user defined
        name in the arkouda server
        
    Raises
    ------
    TypeError
        Raised if user_defined_name is not a str

    See also
    --------
    register_pdarray, unregister_pdarray

    Notes
    -----
    Registered names/pdarrays in the server are immune to deletion 
    until they are unregistered.

    Examples
    --------
    >>> a = zeros(100)
    >>> r_pda = ak.register_pdarray(a, "my_zeros")
    >>> # potentially disconnect from server and reconnect to server
    >>> b = ak.attach_pdarray("my_zeros")
    >>> # ...other work...
    >>> ak.unregister_pdarray(b)
    """
    repMsg = generic_msg(cmd="attach", args="{}".format(user_defined_name))
    return create_pdarray(repMsg)


@typechecked
def unregister_pdarray(pda : Union[str,pdarray]) -> None:
    """
    Unregister a pdarray in the arkouda server which was previously 
    registered using register_pdarray() and/or attached to using attach_pdarray()
    
    Parameters
    ----------
    pda : str or pdarray
        user define name which array was registered under

    Returns
    -------
    None

    Raises 
    ------
    TypeError
        Raised if pda is neither a pdarray nor a str

    See also
    --------
    register_pdarray, unregister_pdarray

    Notes
    -----
    Registered names/pdarrays in the server are immune to deletion until 
    they are unregistered.

    Examples
    --------
    >>> a = zeros(100)
    >>> r_pda = ak.register_pdarray(a, "my_zeros")
    >>> # potentially disconnect from server and reconnect to server
    >>> b = ak.attach_pdarray("my_zeros")
    >>> # ...other work...
    >>> ak.unregister_pdarray(b)
    """
    if isinstance(pda, pdarray):
        repMsg = generic_msg(cmd="unregister", args="{}".format(pda.name))

    if isinstance(pda, str):
        repMsg = generic_msg(cmd="unregister", args="{}".format(pda))
