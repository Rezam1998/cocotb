#!/usr/bin/env python

# Copyright (c) 2013 Potential Ventures Ltd
# Copyright (c) 2013 SolarFlare Communications Inc
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#     * Redistributions of source code must retain the above copyright
#       notice, this list of conditions and the following disclaimer.
#     * Redistributions in binary form must reproduce the above copyright
#       notice, this list of conditions and the following disclaimer in the
#       documentation and/or other materials provided with the distribution.
#     * Neither the name of Potential Ventures Ltd,
#       SolarFlare Communications Inc nor the
#       names of its contributors may be used to endorse or promote products
#       derived from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL POTENTIAL VENTURES LTD BE LIABLE FOR ANY
# DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
# (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
# ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

# -*- coding: utf-8 -*-

import ctypes
import warnings
import collections.abc

import os

if "COCOTB_SIM" in os.environ:
    import simulator
else:
    simulator = None

import cocotb
from cocotb.binary import BinaryValue
from cocotb.log import SimLog
from cocotb.result import TestError

# Only issue a warning for each deprecated attribute access
_deprecation_warned = {}


class SimHandleBase(object):
    """Base class for all simulation objects.

    We maintain a handle which we can use for GPI calls.
    """

    # For backwards compatibility we support a mapping of old member names
    # which may alias with the simulator hierarchy.  In these cases the
    # simulator result takes priority, only falling back to the python member
    # if there is no colliding object in the elaborated design.
    _compat_mapping = {
        "log"               :       "_log",
        "fullname"          :       "_fullname",
        "name"              :       "_name",
        }

    def __init__(self, handle, path):
        """
        .. Constructor. This RST comment works around sphinx-doc/sphinx#6885

        Args:
            handle (int): The GPI handle to the simulator object.
            path (str): Path to this handle, ``None`` if root.
        """
        self._handle = handle
        self._len = None
        self._sub_handles = {}  # Dictionary of children
        self._invalid_sub_handles = {}  # Dictionary of invalid queries

        self._name = simulator.get_name_string(self._handle)
        self._type = simulator.get_type_string(self._handle)
        self._fullname = self._name + "(%s)" % self._type
        self._path = self._name if path is None else path
        self._log = SimLog("cocotb.%s" % self._name)
        self._log.debug("Created")
        self._def_name = simulator.get_definition_name(self._handle)
        self._def_file = simulator.get_definition_file(self._handle)

    def get_definition_name(self):
        return self._def_name

    def get_definition_file(self):
        return self._def_file

    def __hash__(self):
        return self._handle

    def __len__(self):
        """Returns the 'length' of the underlying object.

        For vectors this is the number of bits.
        """
        if self._len is None:
            self._len = simulator.get_num_elems(self._handle)
        return self._len

    def __eq__(self, other):
        """Equality comparator for handles

        Example usage::

            if clk == dut.clk:
                do_something()
        """
        if not isinstance(other, SimHandleBase):
            return NotImplemented
        return self._handle == other._handle

    def __ne__(self, other):
        if not isinstance(other, SimHandleBase):
            return NotImplemented
        return self._handle != other._handle

    def __repr__(self):
        desc = self._path
        defname = self._def_name
        if defname:
            desc += " with definition "+defname
            deffile = self._def_file
            if deffile:
                desc += " (at "+deffile+")"
        return type(self).__name__ + "(" + desc + ")"

    def __str__(self):
        return self._path

    def __setattr__(self, name, value):
        if name in self._compat_mapping:
            if name not in _deprecation_warned:
                warnings.warn("Use of attribute %r is deprecated, use %r instead" % (name, self._compat_mapping[name]))
                _deprecation_warned[name] = True
            return setattr(self, self._compat_mapping[name], value)
        else:
            return object.__setattr__(self, name, value)

    def __getattr__(self, name):
        if name in self._compat_mapping:
            if name not in _deprecation_warned:
                warnings.warn("Use of attribute %r is deprecated, use %r instead" % (name, self._compat_mapping[name]))
                _deprecation_warned[name] = True
            return getattr(self, self._compat_mapping[name])
        else:
            return object.__getattribute__(self, name)


class RegionObject(SimHandleBase):
    """A region object, such as a scope or namespace.

    Region objects don't have values, they are effectively scopes or namespaces.
    """
    def __init__(self, handle, path):
        SimHandleBase.__init__(self, handle, path)
        self._discovered = False

    def __iter__(self):
        """Iterate over all known objects in this layer of hierarchy."""
        if not self._discovered:
            self._discover_all()

        for name, handle in self._sub_handles.items():
            if isinstance(handle, list):
                self._log.debug("Found index list length %d", len(handle))
                for subindex, subhdl in enumerate(handle):
                    if subhdl is None:
                        self._log.warning("Index %d doesn't exist in %s.%s", subindex, self._name, name)
                        continue
                    self._log.debug("Yielding index %d from %s (%s)", subindex, name, type(subhdl))
                    yield subhdl
            else:
                self._log.debug("Yielding %s (%s)", name, handle)
                yield handle

    def _discover_all(self):
        """When iterating or performing tab completion, we run through ahead of
        time and discover all possible children, populating the ``_sub_handles``
        mapping. Hierarchy can't change after elaboration so we only have to
        do this once.
        """
        if self._discovered:
            return
        self._log.debug("Discovering all on %s", self._name)
        for thing in _SimIterator(self._handle, simulator.OBJECTS):
            name = simulator.get_name_string(thing)
            try:
                hdl = SimHandle(thing, self._child_path(name))
            except TestError as e:
                self._log.debug("%s", e)
                continue

            key = self._sub_handle_key(name)

            if key is not None:
                self._sub_handles[key] = hdl
            else:
                self._log.debug("Unable to translate handle >%s< to a valid _sub_handle key", hdl._name)
                continue

        self._discovered = True

    def _child_path(self, name):
        """Returns a string of the path of the child :any:`SimHandle` for a given *name*."""
        return self._path + "." + name

    def _sub_handle_key(self, name):
        """Translates the handle name to a key to use in ``_sub_handles`` dictionary."""
        return name.split(".")[-1]

    def __dir__(self):
        """Permits IPython tab completion to work."""
        self._discover_all()
        return super(RegionObject, self).__dir__() + [str(k) for k in self._sub_handles]


class HierarchyObject(RegionObject):
    """Hierarchy objects are namespace/scope objects."""

    def __setattr__(self, name, value):
        """Provide transparent access to signals via the hierarchy.

        Slightly hacky version of operator overloading in Python.

        Raise an :exc:`AttributeError` if users attempt to create new members which
        don't exist in the design.
        """
        if name.startswith("_"):
            return SimHandleBase.__setattr__(self, name, value)
        if self.__hasattr__(name) is not None:
            sub = self.__getattr__(name)
            sub.value = value
            return
        if name in self._compat_mapping:
            return SimHandleBase.__setattr__(self, name, value)
        raise AttributeError("Attempt to access %s which isn't present in %s" %(
            name, self._name))

    def __getattr__(self, name):
        """Query the simulator for a object with the specified name
        and cache the result to build a tree of objects.
        """
        try:
            return self._sub_handles[name]
        except KeyError:
            pass

        if name.startswith("_"):
            return SimHandleBase.__getattr__(self, name)

        new_handle = simulator.get_handle_by_name(self._handle, name)

        if not new_handle:
            if name in self._compat_mapping:
                return SimHandleBase.__getattr__(self, name)
            raise AttributeError("%s contains no object named %s" % (self._name, name))

        sub_handle = SimHandle(new_handle, self._child_path(name))
        self._sub_handles[name] = sub_handle
        return sub_handle

    def __hasattr__(self, name):
        """Since calling ``hasattr(handle, "something")`` will print out a
        backtrace to the log (since usually attempting to access a
        non-existent member is an error) we provide a 'peek' function.

        We still add the found handle to our dictionary to prevent leaking
        handles.
        """
        if name in self._sub_handles:
            return self._sub_handles[name]

        if name in self._invalid_sub_handles:
            return None

        new_handle = simulator.get_handle_by_name(self._handle, name)
        if new_handle:
            self._sub_handles[name] = SimHandle(new_handle, self._child_path(name))
        else:
            self._invalid_sub_handles[name] = None
        return new_handle

    def _id(self, name, extended=True):
        """Query the simulator for a object with the specified name,
        including extended identifiers,
        and cache the result to build a tree of objects.
        """
        if extended:
            name = "\\"+name+"\\"

        if self.__hasattr__(name) is not None:
            return getattr(self, name)
        raise AttributeError("%s contains no object named %s" % (self._name, name))

class HierarchyArrayObject(RegionObject):
    """Hierarchy Arrays are containers of Hierarchy Objects."""

    def _sub_handle_key(self, name):
        """Translates the handle name to a key to use in ``_sub_handles`` dictionary."""
        # This is slightly hacky, but we need to extract the index from the name
        #
        # FLI and VHPI(IUS):  _name(X) where X is the index
        # VHPI(ALDEC):        _name__X where X is the index
        # VPI:                _name[X] where X is the index
        import re
        result = re.match(r"{0}__(?P<index>\d+)$".format(self._name), name)
        if not result:
            result = re.match(r"{0}\((?P<index>\d+)\)$".format(self._name), name)
        if not result:
            result = re.match(r"{0}\[(?P<index>\d+)\]$".format(self._name), name)

        if result:
            return int(result.group("index"))
        else:
            self._log.error("Unable to match an index pattern: %s", name)
            return None

    def __len__(self):
        """Returns the 'length' of the generate block."""
        if self._len is None:
            if not self._discovered:
                self._discover_all()

            self._len = len(self._sub_handles)
        return self._len

    def __getitem__(self, index):
        if isinstance(index, slice):
            raise IndexError("Slice indexing is not supported")
        if index in self._sub_handles:
            return self._sub_handles[index]
        new_handle = simulator.get_handle_by_index(self._handle, index)
        if not new_handle:
            raise IndexError("%s contains no object at index %d" % (self._name, index))
        path = self._path + "[" + str(index) + "]"
        self._sub_handles[index] = SimHandle(new_handle, path)
        return self._sub_handles[index]

    def _child_path(self, name):
        """Returns a string of the path of the child :any:`SimHandle` for a given name."""
        index = self._sub_handle_key(name)
        return self._path + "[" + str(index) + "]"

    def __setitem__(self, index, value):
        raise TypeError("Not permissible to set %s at index %d" % (self._name, index))


class _AssignmentResult(object):
    """
    An object that exists solely to provide an error message if the caller
    is not aware of cocotb's meaning of ``<=``.
    """
    def __init__(self, signal, value):
        self._signal = signal
        self._value = value

    def __bool__(self):
        raise TypeError(
            "Attempted to use `{0._signal!r} <= {0._value!r}` (a cocotb "
            "delayed write) as if it were a numeric comparison. To perform "
            "comparison, use `{0._signal!r}.value <= {0._value!r}` instead."
            .format(self)
        )


class NonHierarchyObject(SimHandleBase):
    """Common base class for all non-hierarchy objects."""

    def __iter__(self):
        return iter(())

    @property
    def value(self):
        raise TypeError("Not permissible to get values of object %s of type %s" % (self._name, type(self)))

    def setimmediatevalue(self, value):
        raise TypeError("Not permissible to set values on object %s of type %s" % (self._name, type(self)))

    @value.setter
    def value(self, value):
        raise TypeError("Not permissible to set values on object %s of type %s" % (self._name, type(self)))

    def __le__(self, value):
        """Overload less-than-or-equal-to operator to provide an HDL-like shortcut.

        Example:
        >>> module.signal <= 2
        """
        self.value = value
        return _AssignmentResult(self, value)

    def __eq__(self, other):
        """Equality comparator for non-hierarchy objects

        If ``other`` is not a :class:`SimHandleBase` instance the comparision
        uses the comparison method of the ``other`` object against our
        ``.value``.
        """
        if isinstance(other, SimHandleBase):
            return SimHandleBase.__eq__(self, other)
        return self.value == other

    def __ne__(self, other):
        if isinstance(other, SimHandleBase):
            return SimHandleBase.__ne__(self, other)
        return self.value != other

    # Re-define hash because we defined __eq__
    def __hash__(self):
        return SimHandleBase.__hash__(self)


class ConstantObject(NonHierarchyObject):
    """An object which has a value that can be read, but not set.

    The value is cached in the class since it is fixed at elaboration
    time and won't change within a simulation.
    """
    def __init__(self, handle, path, handle_type):
        """
        Args:
            handle (int): The GPI handle to the simulator object.
            path (str): Path to this handle, ``None`` if root.
            handle_type: The type of the handle
                (``simulator.INTEGER``, ``simulator.ENUM``,
                ``simulator.REAL``, ``simulator.STRING``).
        """
        NonHierarchyObject.__init__(self, handle, path)
        if handle_type in [simulator.INTEGER, simulator.ENUM]:
            self._value = simulator.get_signal_val_long(self._handle)
        elif handle_type == simulator.REAL:
            self._value = simulator.get_signal_val_real(self._handle)
        elif handle_type == simulator.STRING:
            self._value = simulator.get_signal_val_str(self._handle)
        else:
            val = simulator.get_signal_val_binstr(self._handle)
            self._value = BinaryValue(n_bits=len(val))
            try:
                self._value.binstr = val
            except Exception:
                self._value = val

    def __int__(self):
        return int(self.value)

    def __float__(self):
        return float(self.value)

    @NonHierarchyObject.value.getter
    def value(self):
        """The value of this simulation object."""
        return self._value

    def __str__(self):
        return str(self.value)


class NonHierarchyIndexableObject(NonHierarchyObject):
    """ A non-hierarchy indexable object. """
    def __init__(self, handle, path):
        NonHierarchyObject.__init__(self, handle, path)
        self._range = simulator.get_range(self._handle)

    def __setitem__(self, index, value):
        """Provide transparent assignment to indexed array handles."""
        self[index].value = value

    def __getitem__(self, index):
        if isinstance(index, slice):
            raise IndexError("Slice indexing is not supported")
        if self._range is None:
            raise IndexError("%s is not indexable.  Unable to get object at index %d" % (self._fullname, index))
        if index in self._sub_handles:
            return self._sub_handles[index]
        new_handle = simulator.get_handle_by_index(self._handle, index)
        if not new_handle:
            raise IndexError("%s contains no object at index %d" % (self._fullname, index))
        path = self._path + "[" + str(index) + "]"
        self._sub_handles[index] = SimHandle(new_handle, path)
        return self._sub_handles[index]

    def __iter__(self):
        if self._range is None:
            return

        self._log.debug("Iterating with range [%d:%d]", self._range[0], self._range[1])
        for i in self._range_iter(self._range[0], self._range[1]):
            try:
                result = self[i]
                yield result
            except IndexError:
                continue

    def _range_iter(self, left, right):
        if left > right:
            while left >= right:
                yield left
                left = left - 1
        else:
            while left <= right:
                yield left
                left = left + 1

    @NonHierarchyObject.value.getter
    def value(self):
        """A list of each value within this simulation object.

        Getting and setting the current value of an array is done
        by iterating through sub-handles in left-to-right order.

        Given an HDL array ``arr``:

        +--------------+---------------------+--------------------------------------------------------------+
        | Verilog      | VHDL                | ``arr.value`` is equivalent to                               |
        +==============+=====================+==============================================================+
        | ``arr[4:7]`` | ``arr(4 to 7)``     | ``[arr[4].value, arr[5].value, arr[6].value, arr[7].value]`` |
        +--------------+---------------------+--------------------------------------------------------------+
        | ``arr[7:4]`` | ``arr(7 downto 4)`` | ``[arr[7].value, arr[6].value, arr[5].value, arr[4].value]`` |
        +--------------+---------------------+--------------------------------------------------------------+

        When setting this property as in ``arr.value = ...``, the same index equivalence as noted in the table holds.

        .. note::
            When setting this property, the values will be cached as explained in :attr:`ModifiableObject.value`.

        .. warning::
            Assigning a value to a sub-handle:

            - **Wrong**: ``dut.some_array.value[0] = 1`` (gets value as a list then updates index 0)
            - **Correct**: ``dut.some_array[0].value = 1``
        """
        # Don't use self.__iter__, because it has an unwanted `except IndexError`
        return [
            self[i].value
            for i in self._range_iter(self._range[0], self._range[1])
        ]

    @value.setter
    def value(self, value):
        """Assign value from a list of same length to an array in left-to-right order.
        Index 0 of the list maps to the left-most index in the array.

        See the docstring for :attr:`value` above.
        """
        if type(value) is not list:
            raise TypeError("Assigning non-list value to object %s of type %s" % (self._name, type(self)))
        if len(value) != len(self):
            raise ValueError("Assigning list of length %d to object %s of length %d" % (
                len(value), self._name, len(self)))
        for val_idx, self_idx in enumerate(self._range_iter(self._range[0], self._range[1])):
            self[self_idx].value = value[val_idx]


class _SimIterator(collections.abc.Iterator):
    """Iterator over simulator objects. For internal use only."""

    def __init__(self, handle, mode):
        self._iter = simulator.iterate(handle, mode)

    def __next__(self):
        return simulator.next(self._iter)


class NonConstantObject(NonHierarchyIndexableObject):
    """ A non-constant object"""
    # FIXME: what is the difference to ModifiableObject? Explain in docstring.

    def drivers(self):
        """An iterator for gathering all drivers for a signal."""
        return _SimIterator(self._handle, simulator.DRIVERS)

    def loads(self):
        """An iterator for gathering all loads on a signal."""
        return _SimIterator(self._handle, simulator.LOADS)

class _SetAction:
    """Base class representing the type of action used while write-accessing a handle."""
    pass

class _SetValueAction(_SetAction):
    __slots__ = ("value",)
    """Base class representing the type of action used while write-accessing a handle with a value."""
    def __init__(self, value):
        self.value = value

class Deposit(_SetValueAction):
    """Action used for placing a value into a given handle."""
    def _as_gpi_args_for(self, hdl):
        return self.value, 0  # GPI_DEPOSIT

class Force(_SetValueAction):
    """Action used to force a handle to a given value until a release is applied."""
    def _as_gpi_args_for(self, hdl):
        return self.value, 1  # GPI_FORCE

class Freeze(_SetAction):
    """Action used to make a handle keep its current value until a release is used."""
    def _as_gpi_args_for(self, hdl):
        return hdl.value, 1  # GPI_FORCE

class Release(_SetAction):
    """Action used to stop the effects of a previously applied force/freeze action."""
    def _as_gpi_args_for(self, hdl):
        return 0, 2  # GPI_RELEASE

class ModifiableObject(NonConstantObject):
    """Base class for simulator objects whose values can be modified."""

    def setimmediatevalue(self, value):
        """Set the value of the underlying simulation object to *value*.

        This operation will fail unless the handle refers to a modifiable
        object, e.g. net, signal or variable.

        We determine the library call to make based on the type of the value
        because assigning integers less than 32 bits is faster.

        Args:
            value (ctypes.Structure, cocotb.binary.BinaryValue, int, double):
                The value to drive onto the simulator object.

        Raises:
            TypeError: If target is not wide enough or has an unsupported type
                 for value assignment.
        """
        value, set_action = self._check_for_set_action(value)

        if isinstance(value, int) and value < 0x7fffffff and len(self) <= 32:
            simulator.set_signal_val_long(self._handle, set_action, value)
            return
        if isinstance(value, ctypes.Structure):
            value = BinaryValue(value=cocotb.utils.pack(value), n_bits=len(self))
        elif isinstance(value, int):
            value = BinaryValue(value=value, n_bits=len(self), bigEndian=False)
        elif isinstance(value, dict):
            # We're given a dictionary with a list of values and a bit size...
            num = 0
            vallist = list(value["values"])
            vallist.reverse()
            if len(vallist) * value["bits"] != len(self):
                self._log.critical("Unable to set with array length %d of %d bit entries = %d total, target is only %d bits long",
                                   len(value["values"]), value["bits"], len(value["values"]) * value["bits"], len(self))
                raise TypeError("Unable to set with array length %d of %d bit entries = %d total, target is only %d bits long" %
                                (len(value["values"]), value["bits"], len(value["values"]) * value["bits"], len(self)))

            for val in vallist:
                num = (num << value["bits"]) + val
            value = BinaryValue(value=num, n_bits=len(self), bigEndian=False)

        elif not isinstance(value, BinaryValue):
            self._log.critical("Unsupported type for value assignment: %s (%s)", type(value), repr(value))
            raise TypeError("Unable to set simulator value with type %s" % (type(value)))

        simulator.set_signal_val_binstr(self._handle, set_action, value.binstr)

    def _check_for_set_action(self, value):
        if not isinstance(value, _SetAction):
            return value, 0  # GPI_DEPOSIT
        return value._as_gpi_args_for(self)

    @NonConstantObject.value.getter
    def value(self):
        """The value of this simulation object.

        .. note::
            When setting this property, the value is stored by the :class:`~cocotb.scheduler.Scheduler`
            and all stored values are written at the same time at the end of the current simulator time step.

            Use :meth:`setimmediatevalue` to set the value immediately.
        """
        binstr = simulator.get_signal_val_binstr(self._handle)
        result = BinaryValue(binstr, len(binstr))
        return result

    @value.setter
    def value(self, value):
        """Assign value to this simulation object.

        See the docstring for :attr:`value` above.
        """
        cocotb.scheduler.save_write(self, value)

    def __int__(self):
        return int(self.value)

    def __str__(self):
        return str(self.value)


class RealObject(ModifiableObject):
    """Specific object handle for Real signals and variables."""

    def setimmediatevalue(self, value):
        """Set the value of the underlying simulation object to value.

        This operation will fail unless the handle refers to a modifiable
        object, e.g. net, signal or variable.

        Args:
            value (float): The value to drive onto the simulator object.

        Raises:
            TypeError: If target has an unsupported type for
                real value assignment.
        """
        value, set_action = self._check_for_set_action(value)

        try:
            value = float(value)
        except ValueError:
            self._log.critical("Unsupported type for real value assignment: %s (%s)" %
                               (type(value), repr(value)))
            raise TypeError("Unable to set simulator value with type %s" % (type(value)))

        simulator.set_signal_val_real(self._handle, set_action, value)

    @ModifiableObject.value.getter
    def value(self):
        return simulator.get_signal_val_real(self._handle)

    def __float__(self):
        return float(self.value)


class EnumObject(ModifiableObject):
    """Specific object handle for enumeration signals and variables."""

    def setimmediatevalue(self, value):
        """Set the value of the underlying simulation object to *value*.

        This operation will fail unless the handle refers to a modifiable
        object, e.g. net, signal or variable.

        Args:
            value (int): The value to drive onto the simulator object.

        Raises:
            TypeError: If target has an unsupported type for
                 integer value assignment.
        """
        value, set_action = self._check_for_set_action(value)

        if isinstance(value, BinaryValue):
            value = int(value)
        elif not isinstance(value, int):
            self._log.critical("Unsupported type for integer value assignment: %s (%s)", type(value), repr(value))
            raise TypeError("Unable to set simulator value with type %s" % (type(value)))

        simulator.set_signal_val_long(self._handle, set_action, value)

    @ModifiableObject.value.getter
    def value(self):
        return simulator.get_signal_val_long(self._handle)


class IntegerObject(ModifiableObject):
    """Specific object handle for Integer and Enum signals and variables."""

    def setimmediatevalue(self, value):
        """Set the value of the underlying simulation object to *value*.

        This operation will fail unless the handle refers to a modifiable
        object, e.g. net, signal or variable.

        Args:
            value (int): The value to drive onto the simulator object.

        Raises:
            TypeError: If target has an unsupported type for
                 integer value assignment.
        """
        value, set_action = self._check_for_set_action(value)

        if isinstance(value, BinaryValue):
            value = int(value)
        elif not isinstance(value, int):
            self._log.critical("Unsupported type for integer value assignment: %s (%s)", type(value), repr(value))
            raise TypeError("Unable to set simulator value with type %s" % (type(value)))

        simulator.set_signal_val_long(self._handle, set_action, value)

    @ModifiableObject.value.getter
    def value(self):
        return simulator.get_signal_val_long(self._handle)


class StringObject(ModifiableObject):
    """Specific object handle for String variables."""

    def setimmediatevalue(self, value):
        """Set the value of the underlying simulation object to *value*.

        This operation will fail unless the handle refers to a modifiable
        object, e.g. net, signal or variable.

        Args:
            value (str): The value to drive onto the simulator object.

        Raises:
            TypeError: If target has an unsupported type for
                 string value assignment.
        """
        value, set_action = self._check_for_set_action(value)

        if not isinstance(value, str):
            self._log.critical("Unsupported type for string value assignment: %s (%s)", type(value), repr(value))
            raise TypeError("Unable to set simulator value with type %s" % (type(value)))

        simulator.set_signal_val_str(self._handle, set_action, value)

    @ModifiableObject.value.getter
    def value(self):
        return simulator.get_signal_val_str(self._handle)

_handle2obj = {}

def SimHandle(handle, path=None):
    """Factory function to create the correct type of `SimHandle` object.

    Args:
        handle (int): The GPI handle to the simulator object.
        path (str): Path to this handle, ``None`` if root.

    Returns:
        The `SimHandle` object.

    Raises:
        TestError: If no matching object for GPI type could be found.
    """
    _type2cls = {
        simulator.MODULE:      HierarchyObject,
        simulator.STRUCTURE:   HierarchyObject,
        simulator.REG:         ModifiableObject,
        simulator.NET:         ModifiableObject,
        simulator.NETARRAY:    NonHierarchyIndexableObject,
        simulator.REAL:        RealObject,
        simulator.INTEGER:     IntegerObject,
        simulator.ENUM:        EnumObject,
        simulator.STRING:      StringObject,
        simulator.GENARRAY:    HierarchyArrayObject,
    }

    # Enforce singletons since it's possible to retrieve handles avoiding
    # the hierarchy by getting driver/load information
    global _handle2obj
    try:
        return _handle2obj[handle]
    except KeyError:
        pass

    t = simulator.get_type(handle)

    # Special case for constants
    if simulator.get_const(handle) and t not in [simulator.MODULE,
                                                 simulator.STRUCTURE,
                                                 simulator.NETARRAY,
                                                 simulator.GENARRAY]:
        obj = ConstantObject(handle, path, t)
        _handle2obj[handle] = obj
        return obj

    if t not in _type2cls:
        raise TestError("Couldn't find a matching object for GPI type %d (path=%s)" % (t, path))
    obj = _type2cls[t](handle, path)
    _handle2obj[handle] = obj
    return obj
