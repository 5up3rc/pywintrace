########################################################################
# Copyright 2017 FireEye Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
########################################################################

# Public packages
import threading
import logging
import ctypes as ct
import ctypes.wintypes as wt

# Custom packages
from etw import evntrace as et
from etw import in6addr as ia
from etw import evntcons as ec
from etw import wmistr as ws
from etw import tdh as tdh
from etw.common import rel_ptr_to_str, MAX_UINT, ETWException

logger = logging.getLogger(__name__)


class TraceProperties:
    """
    The TraceProperties class represents the EVENT_TRACE_PROPERTIES structure. The class wraps
    this structure to make it easier to interact with.
    """

    def __init__(self, ring_buf_size=1024, max_str_len=1024, min_buffers=0, max_buffers=0):
        """
        Initializes an EVENT_TRACE_PROPERTIES structure.

        :param ring_buf_size: The size of the ring buffer used for capturing events.
        :param max_str_len: The maximum length of the strings the proceed the structure.
                            Unless you know what you are doing, do not modify this value.
        :param min_buffers: The minimum number of buffers for an event tracing session.
                            Unless you know what you are doing, do not modify this value.
        :param max_buffers: The maximum number of buffers for an event tracing session.
                            Unless you know what you are doing, do not modify this value.
        """
        # In this structure, the LoggerNameOffset and other string fields reside immediately
        # after the EVENT_TRACE_PROPERTIES structure. So allocate enough space for the
        # structure and any strings we are using.
        buf_size = ct.sizeof(et.EVENT_TRACE_PROPERTIES) + 2 * ct.sizeof(ct.c_wchar) * max_str_len

        # noinspection PyCallingNonCallable
        self._buf = (ct.c_char * buf_size)()
        self._props = ct.cast(ct.pointer(self._buf), ct.POINTER(et.EVENT_TRACE_PROPERTIES))

        prop = self._props
        prop.contents.Wnode.BufferSize = buf_size
        prop.contents.BufferSize = ring_buf_size

        if min_buffers != 0:
            prop.contents.MinimumBuffers = min_buffers

        if max_buffers != 0:
            prop.contents.MaximumBuffers = max_buffers

        prop.contents.Wnode.Flags = ws.WNODE_FLAG_TRACED_GUID
        prop.contents.LogFileMode = et.EVENT_TRACE_REAL_TIME_MODE
        prop.contents.LoggerNameOffset = ct.sizeof(et.EVENT_TRACE_PROPERTIES)

    def get(self):
        """
        This class wraps the construction of a struct for ctypes. As a result, in order to properly use it as a ctypes
        structure, you must use the private field _props. To maintain proper encapsulation, this getter is used to
        retrieve this value when needed.

        :return: The _props field needed for using this class as a ctypes EVENT_TRACE_PROPERTIES structure.
        """
        return self._props


class EventProvider:
    """
    Wraps all interactions with Event Tracing for Windows (ETW) event providers. This includes
    starting and stopping them.

    N.B. If using this class, do not call start() and stop() directly. Only use through via ctxmgr
    """

    def __init__(
            self,
            provider_guid,
            session_name,
            session_properties,
            level=et.TRACE_LEVEL_INFORMATION,
            match_any_bitmask=0,
            match_all_bitmask=0):
        """
        Sets the appropriate values for an ETW provider.

        :param provider_guid: The GUID of the provider that we want to start
        :param session_name: The name of the provider session.
        :param session_properties: A TraceProperties instance used to specify the parameters for the provider
        :param level: The logging level desired.
        :param match_any_bitmask: Bit mask of flags for the any match keywords.
        :param match_all_bitmask: Bit mask of flags for the all match keywords.
        """
        self.provider_guid = provider_guid
        self.session_name = session_name
        self.session_properties = session_properties
        self.session_handle = et.TRACEHANDLE()
        self.level = level
        self.match_any_bitmask = match_any_bitmask
        self.match_all_bitmask = match_all_bitmask

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc, ex, tb):
        self.stop()

    def start(self):
        """
        Wraps the necessary processes needed for starting an ETW provider session.

        :return:  Does not return anything.
        """
        status = et.StartTraceW(ct.byref(self.session_handle), self.session_name, self.session_properties.get())
        if status != tdh.ERROR_SUCCESS:
            raise ct.WinError()

        status = et.EnableTraceEx2(self.session_handle,
                                   ct.byref(self.provider_guid),
                                   et.EVENT_CONTROL_CODE_ENABLE_PROVIDER,
                                   self.level,
                                   self.match_any_bitmask,
                                   self.match_all_bitmask,
                                   0,
                                   None)
        if status != tdh.ERROR_SUCCESS:
            raise ct.WinError()

    def stop(self):
        """
        Wraps the necessary processes needed for stopping an ETW provider session.

        :return: Does not return anything
        """
        if self.session_handle.value == 0:
            return
        status = et.EnableTraceEx2(self.session_handle,
                                   ct.byref(self.provider_guid),
                                   et.EVENT_CONTROL_CODE_DISABLE_PROVIDER,
                                   self.level,
                                   self.match_any_bitmask,
                                   self.match_all_bitmask,
                                   0,
                                   None)
        if status != tdh.ERROR_SUCCESS:
            raise ct.WinError()

        status = et.ControlTraceW(self.session_handle,
                                  self.session_name,
                                  self.session_properties.get(),
                                  et.EVENT_TRACE_CONTROL_STOP)
        if status != tdh.ERROR_SUCCESS:
            raise ct.WinError()


class EventConsumer:
    """
    Wraps all interactions with Event Tracing for Windows (ETW) event consumers. This includes
    starting and stopping the consumer. Additionally, each consumer begins processing events in
    a separate thread and uses a callback to process any events it receives in this thread -- those
    methods are implemented here as well.

    N.B. If using this class, do not call start() and stop() directly. Only use through via ctxmgr
    """

    def __init__(self, logger_name, event_callback, task_name_filters):
        """
        Initializes a real time event consumer object.

        :param logger_name: The name of the session that we want to consume events from.
        :param event_callback: The optional callback function which can be used to return the values.
        """
        self.trace_handle = None
        self.process_thread = None
        self.logger_name = logger_name
        self.end_capture = threading.Event()
        self.event_callback = event_callback
        self.vfield_length = None
        self.index = 0
        self.task_name_filters = task_name_filters

        # Construct the EVENT_TRACE_LOGFILE structure
        self.logfile = et.EVENT_TRACE_LOGFILE()
        self.logfile.LoggerName = logger_name
        self.logfile.ProcessTraceMode = (ec.PROCESS_TRACE_MODE_REAL_TIME | ec.PROCESS_TRACE_MODE_EVENT_RECORD)
        self.logfile.EventRecordCallback = et.EVENT_RECORD_CALLBACK(self._processEvent)

    def __enter__(self):
        self.start()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()

    def start(self):
        """
        Starts a trace consumer.

        :return: Returns True on Success or False on Failure
        """
        self.trace_handle = et.OpenTraceW(ct.byref(self.logfile))
        if self.trace_handle == et.INVALID_PROCESSTRACE_HANDLE:
            raise ct.WinError()

        # For whatever reason, the restype is ignored
        self.trace_handle = et.TRACEHANDLE(self.trace_handle)
        self.process_thread = threading.Thread(target=self._run, args=(self.trace_handle, self.end_capture))
        self.process_thread.start()

    def stop(self):
        """
        Stops a trace consumer.

        :return: Returns True on Success or False on Failure
        """
        # Signal to the thread that we are reading to stop processing events.
        self.end_capture.set()

        # Call CloseTrace to cause ProcessTrace to return (unblock)
        et.CloseTrace(self.trace_handle)

        # If ProcessThread is actively parsing an event, we want to give it a chance to finish
        # before pulling the rug out from underneath it.
        self.process_thread.join()

    @staticmethod
    def _run(trace_handle, end_capture):
        """
        Because ProcessTrace() blocks, this function is used to spin off new threads.

        :param trace_handle: The handle for the trace consumer that we want to begin processing.
        :param end_capture: A callback function which determines what should be done with the results.
        :return: Does not return a value.
        """
        while True:
            if tdh.ERROR_SUCCESS != et.ProcessTrace(ct.byref(trace_handle), 1, None, None):
                end_capture.set()

            if end_capture.isSet():
                break

    @staticmethod
    def _getEventInformation(record):
        """
        Initially we are handed an EVENT_RECORD structure. While this structure technically contains
        all of the information necessary, TdhGetEventInformation parses the structure and simplifies it
        so we can more effectively parse and handle the various fields.

        :param record: The EventRecord structure for the event we are parsing
        :return: Returns a pointer to a TRACE_EVENT_INFO structure or None on error.
        """
        info = ct.POINTER(tdh.TRACE_EVENT_INFO)()
        buffer_size = wt.DWORD()

        # Call TdhGetEventInformation once to get the required buffer size and again to actually populate the structure.
        status = tdh.TdhGetEventInformation(record, 0, None, None, ct.byref(buffer_size))
        if tdh.ERROR_INSUFFICIENT_BUFFER == status:
            info = ct.cast((ct.c_byte * buffer_size.value)(), ct.POINTER(tdh.TRACE_EVENT_INFO))
            status = tdh.TdhGetEventInformation(record, 0, None, info, ct.byref(buffer_size))

        # If no scheme is found, return None
        if tdh.ERROR_NOT_FOUND == status:
            logger.warning('Event scheme not found')
            return None

        if tdh.ERROR_SUCCESS != status:
            raise ct.WinError()

        return info

    @staticmethod
    def _getArraySize(record, info, event_property):
        """
        Some of the properties encountered when parsing represent an array of values. This function
        will retrieve the size of the array.

        :param record: The EventRecord structure for the event we are parsing
        :param info: The TraceEventInfo structure for the event we are parsing
        :param event_property: The EVENT_PROPERTY_INFO structure for the TopLevelProperty of the event we are parsing
        :return: Returns a DWORD representing the size of the array or None on error.
        """
        event_property_array = ct.cast(info.contents.EventPropertyInfoArray, ct.POINTER(tdh.EVENT_PROPERTY_INFO))
        flags = event_property.Flags

        if flags & tdh.PropertyParamCount:
            data_descriptor = tdh.PROPERTY_DATA_DESCRIPTOR()
            j = event_property.epi_u2.countPropertyIndex
            property_size = wt.DWORD()
            count = wt.DWORD()

            data_descriptor.PropertyName = info + event_property_array[j].NameOffset
            data_descriptor.ArrayIndex = MAX_UINT

            status = tdh.TdhGetPropertySize(record, 0, None, 1, ct.byref(data_descriptor), ct.byref(property_size))
            if tdh.ERROR_SUCCESS != status:
                raise ct.WinError()

            status = tdh.TdhGetProperty(record, 0, None, 1, ct.byref(data_descriptor), property_size, ct.byref(count))
            if tdh.ERROR_SUCCESS != status:
                raise ct.WinError()
            return count

        if flags & tdh.PropertyParamFixedCount:
            raise ETWException('PropertyParamFixedCount not supported')

        return event_property.epi_u2.count

    @staticmethod
    def _getPropertyLength(record, info, event_property):
        """
        Each property encountered when parsing the top level property has an associated length. If the
        length is available, retrieve it here. In some cases, the length is 0. This can signify that
        we are dealing with a variable length field such as a structure, an IPV6 data, or a string.

        :param record: The EventRecord structure for the event we are parsing
        :param info: The TraceEventInfo structure for the event we are parsing
        :param event_property: The EVENT_PROPERTY_INFO structure for the TopLevelProperty of the event we are parsing
        :return: Returns the length of the property as a c_ulong() or None on error
        """
        flags = event_property.Flags

        if flags & tdh.PropertyParamLength:
            data_descriptor = tdh.PROPERTY_DATA_DESCRIPTOR()
            event_property_array = ct.cast(info.contents.EventPropertyInfoArray, ct.POINTER(tdh.EVENT_PROPERTY_INFO))
            j = wt.DWORD(event_property.epi_u3.length)
            property_size = ct.c_ulong()
            length = wt.DWORD()

            # Setup the PROPERTY_DATA_DESCRIPTOR structure
            data_descriptor.PropertyName = (ct.cast(info, ct.c_voidp).value + event_property_array[j.value].NameOffset)
            data_descriptor.ArrayIndex = MAX_UINT

            status = tdh.TdhGetPropertySize(record, 0, None, 1, ct.byref(data_descriptor), ct.byref(property_size))
            if tdh.ERROR_SUCCESS != status:
                raise ct.WinError()

            status = tdh.TdhGetProperty(record,
                                        0,
                                        None,
                                        1,
                                        ct.byref(data_descriptor),
                                        property_size,
                                        ct.cast(ct.byref(length), ct.POINTER(ct.c_byte)))
            if tdh.ERROR_SUCCESS != status:
                raise ct.WinError()
            return length.value

        in_type = event_property.epi_u1.nonStructType.InType
        out_type = event_property.epi_u1.nonStructType.OutType

        # This is a special case in which the input and output types dictate the size
        if (in_type == tdh.TDH_INTYPE_BINARY) and (out_type == tdh.TDH_OUTTYPE_IPV6):
            return ct.sizeof(ia.IN6_ADDR)

        return event_property.epi_u3.length

    @staticmethod
    def _getMapInfo(record, info, event_property):
        """
        When parsing a field in the event property structure, there may be a mapping between a given
        name and the structure it represents. If it exists, we retrieve that mapping here.

        Because this may legitimately return a NULL value we return a tuple containing the success or
        failure status as well as either None (NULL) or an EVENT_MAP_INFO pointer.

        :param record: The EventRecord structure for the event we are parsing
        :param info: The TraceEventInfo structure for the event we are parsing
        :param event_property: The EVENT_PROPERTY_INFO structure for the TopLevelProperty of the event we are parsing
        :return: A tuple of the map_info structure and boolean indicating whether we succeeded or not
        """
        map_name = rel_ptr_to_str(info, event_property.epi_u1.nonStructType.MapNameOffset)
        map_size = wt.DWORD()
        map_info = ct.POINTER(tdh.EVENT_MAP_INFO)()

        status = tdh.TdhGetEventMapInformation(record, map_name, None, ct.byref(map_size))
        if tdh.ERROR_INSUFFICIENT_BUFFER == status:
            map_info = ct.cast((ct.c_char * map_size.value)(), ct.POINTER(tdh.EVENT_MAP_INFO))
            status = tdh.TdhGetEventMapInformation(record, map_name, map_info, ct.byref(map_size))

        if tdh.ERROR_SUCCESS == status:
            return map_info, True

        # ERROR_NOT_FOUND is actually a perfectly acceptable status
        if tdh.ERROR_NOT_FOUND == status:
            return None, True

        # We actually failed.
        raise ct.WinError()

    @staticmethod
    def _handleEvtInvalidEvtData(user_data, user_data_remaining):
        """
        In this instance, the amount of data we are told to parse exceeds the amount of data that is left in the
        user data appended to the structure. As viewed in Microsoft Message Analyzer, this appears to be commonly
        referred to as a fragment. In this case, we simply copy the data to a new buffer, add a NULL terminating
        character on to the end and call TdhFormatProperty again.

        :param user_data: A pointer to the user data for the specified segment
        :param user_data_remaining: The amount of data that is actually left
        :return: A tuple of the amount consumed and the data itself
        """
        # Instantiate a buffer with enough space for everything plus the NULL terminating character.
        buf = (ct.c_char * (user_data_remaining + ct.sizeof(ct.c_wchar)))()

        # Move the data to the new buffer and NULL terminate it.
        ct.memmove(buf, user_data, user_data_remaining)

        user_data_consumed = ct.c_ushort(user_data_remaining)

        return user_data_consumed, buf

    def _unpackSimpleType(self, record, info, event_property):
        """
        This method handles dumping all simple types of data (i.e., non-struct types).

        :param record: The EventRecord structure for the event we are parsing
        :param info: The TraceEventInfo structure for the event we are parsing
        :param event_property: The EVENT_PROPERTY_INFO structure for the TopLevelProperty of the event we are parsing
        :return: Returns a key-value pair as a dictionary. If we fail, the dictionary is {}
        """
        # Get the EVENT_MAP_INFO, if it is present.
        map_info, success = self._getMapInfo(record, info, event_property)
        if not success:
            return {}

        # Get the length of the value of the property we are dealing with.
        property_length = self._getPropertyLength(record, info, event_property)
        if property_length is None:
            return {}
        # The version of the Python interpreter may be different than the system architecture.
        if record.contents.EventHeader.Flags & ec.EVENT_HEADER_FLAG_32_BIT_HEADER:
            ptr_size = 4
        else:
            ptr_size = 8

        name_field = rel_ptr_to_str(info, event_property.NameOffset)
        if property_length == 0 and self.vfield_length is not None:
            if self.vfield_length == 0:
                self.vfield_length = None
                return {name_field: None}

            # If vfield_length isn't 0, we should be able to parse the property.
            property_length = self.vfield_length

        # After calling the TdhFormatProperty function, use the UserDataConsumed parameter value to set the new values
        # of the UserData and UserDataLength parameters (Subtract UserDataConsumed from UserDataLength and use
        # UserDataLength to increment the UserData pointer).

        # All of the variables needed to actually use TdhFormatProperty retrieve the value
        user_data = record.contents.UserData + self.index
        user_data_remaining = record.contents.UserDataLength - self.index

        # if there is no data remaining then return
        if user_data_remaining <= 0:
            return {}

        in_type = event_property.epi_u1.nonStructType.InType
        out_type = event_property.epi_u1.nonStructType.OutType
        formatted_data_size = wt.DWORD()
        formatted_data = wt.LPWSTR()
        user_data_consumed = ct.c_ushort()

        status = tdh.TdhFormatProperty(info,
                                       map_info,
                                       ptr_size,
                                       in_type,
                                       out_type,
                                       ct.c_ushort(property_length),
                                       user_data_remaining,
                                       ct.cast(user_data, ct.POINTER(ct.c_byte)),
                                       ct.byref(formatted_data_size),
                                       None,
                                       ct.byref(user_data_consumed))

        if status == tdh.ERROR_INSUFFICIENT_BUFFER:
            formatted_data = ct.cast((ct.c_char * formatted_data_size.value)(), wt.LPWSTR)
            status = tdh.TdhFormatProperty(info,
                                           map_info,
                                           ptr_size,
                                           in_type,
                                           out_type,
                                           ct.c_ushort(property_length),
                                           user_data_remaining,
                                           ct.cast(user_data, ct.POINTER(ct.c_byte)),
                                           ct.byref(formatted_data_size),
                                           formatted_data,
                                           ct.byref(user_data_consumed))

        if status != tdh.ERROR_SUCCESS:
            if status != tdh.ERROR_EVT_INVALID_EVENT_DATA:
                raise ct.WinError(status)

            # We can handle this error and still capture the data.
            user_data_consumed, formatted_data = self._handleEvtInvalidEvtData(user_data, user_data_remaining)

        # Increment where we are in the user data segment that we are parsing.
        self.index += user_data_consumed.value

        if name_field.lower().endswith('length'):
            try:
                self.vfield_length = int(formatted_data.value, 10)
            except ValueError:
                logger.warning('Setting vfield_length to None')
                self.vfield_length = None

        data = formatted_data.value
        # Convert the formatted data if necessary
        if out_type in tdh.TDH_CONVERTER_LOOKUP:
            data = tdh.TDH_CONVERTER_LOOKUP[out_type](data)

        return {name_field: data}

    def _unpackComplexType(self, record, info, event_property):
        """
        A complex type (e.g., a structure with sub-properties) can only contain simple types. Loop over all
        sub-properties and dump the property name and value.

        :param record: The EventRecord structure for the event we are parsing
        :param info: The TraceEventInfo structure for the event we are parsing
        :param event_property: The EVENT_PROPERTY_INFO structure for the TopLevelProperty of the event we are parsing
        :return: A dictionary of the property and value for the event we are parsing
        """
        out = {}

        array_size = self._getArraySize(record, info, event_property)
        if array_size is None:
            return {}

        for i in range(array_size):
            start_index = event_property.epi_u1.structType.StructStartIndex
            last_member = start_index + event_property.epi_u1.structType.NumOfStructMembers

            for j in range(start_index, last_member):
                # Because we are no longer dealing with the TopLevelProperty, we need to get the event_property_array
                # again so we can get the EVENT_PROPERTY_INFO structure of the sub-property we are currently parsing.
                event_property_array = ct.cast(info.contents.EventPropertyInfoArray,
                                               ct.POINTER(tdh.EVENT_PROPERTY_INFO))

                key, value = self._unpackSimpleType(record, info, event_property_array[j])
                if key is None and value is None:
                    break

                out[key] = value

        return out

    def _processEvent(self, record):
        """
        This is a callback function that fires whenever an event needs handling. It iterates through the structure to
        parse the properties of each event. If a user defined callback is specified it then passes the parsed data to
        it.


        :param record: The EventRecord structure for the event we are parsing
        :return: Nothing
        """
        info = self._getEventInformation(record)
        if info is None:
            return

        # Some events do not have an associated task_name value. In this case, we should use the provider name instead.
        if info.contents.TaskNameOffset == 0:
            task_name = rel_ptr_to_str(info, info.contents.ProviderNameOffset)
        else:
            task_name = rel_ptr_to_str(info, info.contents.TaskNameOffset)

        task_name = task_name.strip().upper()

        # Add a description for the event
        description = rel_ptr_to_str(info, info.contents.EventMessageOffset)

        # Add the EventID
        event_id = info.contents.EventDescriptor.Id

        # Windows 7 does not support predicate filters. Instead, we use a whitelist to filter things on the consumer.
        if self.task_name_filters and task_name not in self.task_name_filters:
            return

        # add all header fields from EVENT_HEADER structure
        # https://msdn.microsoft.com/en-us/library/windows/desktop/aa363759(v=vs.85).aspx
        out = {'EventHeader': {
            'Size': record.contents.EventHeader.Size,
            'HeaderType': record.contents.EventHeader.HeaderType,
            'Flags': record.contents.EventHeader.Flags,
            'EventProperty': record.contents.EventHeader.EventProperty,
            'ThreadId': record.contents.EventHeader.ThreadId,
            'ProcessId': record.contents.EventHeader.ProcessId,
            'TimeStamp': record.contents.EventHeader.TimeStamp,
            'ProviderId': str(record.contents.EventHeader.ProviderId),
            'EventDescriptor': {'Id': record.contents.EventHeader.EventDescriptor.Id,
                                'Version': record.contents.EventHeader.EventDescriptor.Version,
                                'Channel': record.contents.EventHeader.EventDescriptor.Channel,
                                'Level': record.contents.EventHeader.EventDescriptor.Level,
                                'Opcode': record.contents.EventHeader.EventDescriptor.Opcode,
                                'Task': record.contents.EventHeader.EventDescriptor.Task,
                                'Keyword':
                                    record.contents.EventHeader.EventDescriptor.Keyword},
            'KernelTime': record.contents.EventHeader.KernelTime,
            'UserTime': record.contents.EventHeader.UserTime,
            'ActivityId': str(record.contents.EventHeader.ActivityId)}}

        user_data = record.contents.UserData
        if user_data is None:
            user_data = 0

        end_of_user_data = user_data + record.contents.UserDataLength
        self.index = 0
        self.vfield_length = None
        property_array = ct.cast(info.contents.EventPropertyInfoArray, ct.POINTER(tdh.EVENT_PROPERTY_INFO))

        for i in range(info.contents.TopLevelPropertyCount):
            # If the user_data is the same value as the end_of_user_data, we are ending with a 0-length
            # field. Though not documented, this is completely valid.
            if user_data == end_of_user_data:
                break

            # Determine whether we are processing a simple type or a complex type and act accordingly
            if property_array[i].Flags & tdh.PropertyStruct:
                out.update(self._unpackComplexType(record, info, property_array[i]))
                continue

            out.update(self._unpackSimpleType(record, info, property_array[i]))

        # Add the description field in
        out['Description'] = description
        out['Task Name'] = task_name

        # Call the user's specified callback function
        if self.event_callback:
            self.event_callback((event_id, out))

        return


class ETW:
    """
    Serves as a base class for each capture trace type.
    """

    def __init__(
            self,
            guid,
            ring_buf_size=1024,
            max_str_len=1024,
            min_buffers=0,
            max_buffers=0,
            level=et.TRACE_LEVEL_INFORMATION,
            any_keywords=None,
            all_keywords=None):
        """
        Initializes an instance of the ETW class. The default buffer parameters represent a very typical use case and
        should not be overridden unless the user knows what they are doing.

        :param guid: The dict of the provider to capture ETW data from.
        :param ring_buf_size: The size of the ring buffer used for capturing events.
        :param max_str_len: The maximum length of the strings the proceed the structure.
                            Unless you know what you are doing, do not modify this value.
        :param min_buffers: The minimum number of buffers for an event tracing session.
                            Unless you know what you are doing, do not modify this value.
        :param max_buffers: The maximum number of buffers for an event tracing session.
                            Unless you know what you are doing, do not modify this value.
        :param level: Logging level
        :param any_keywords: List of keywords to match
        :param all_keywords: List of keywords that all must match
        """

        if any_keywords is None:
            any_keywords = []

        if all_keywords is None:
            all_keywords = []

        self.ring_buf_size = ring_buf_size
        self.max_str_len = max_str_len
        self.min_buffers = min_buffers
        self.max_buffers = max_buffers

        self.providers = []
        self.consumers = []
        self.level = level

        name, guid = list(guid.items())[0]
        any_bitmask = get_keywords_bitmask(guid, any_keywords)
        all_bitmask = get_keywords_bitmask(guid, all_keywords)
        self.guids = {name: (guid, any_bitmask, all_bitmask)}

    def start(self, event_callback=None, task_name_filters=None, ignore_exists_error=True):
        """
        Starts the providers and the consumers for capturing data using ETW.

        :param event_callback: An optional parameter allowing the caller to specify a callback function for each event
                               that is parsed.
        :param task_name_filters: List of filters to apply to the ETW capture
        :param ignore_exists_error: If true (default), the library will ignore an ERROR_ALREADY_EXISTS on the
                                    EventProvider start.
        :return: Does not return anything.
        """
        if task_name_filters is None:
            task_name_filters = []

        for guid_name, (guid, any_bitmask, all_bitmask) in self.guids.items():
            # Start the provider
            properties = TraceProperties(self.ring_buf_size, self.max_str_len, self.min_buffers, self.max_buffers)
            provider = EventProvider(guid, guid_name, properties, self.level, any_bitmask, all_bitmask)
            try:
                provider.start()
                self.providers.append(provider)
            except WindowsError as wex:
                if ct.GetLastError() == tdh.ERROR_ALREADY_EXISTS and not ignore_exists_error:
                    raise wex

            # Start the consumer
            consumer = EventConsumer(guid_name, event_callback, task_name_filters)
            consumer.start()
            self.consumers.append(consumer)

    def stop(self):
        """
        Stops the current consumers and providers.

        :return: Does not return anything.
        """

        for provider in list(self.providers):
            provider.stop()
            self.providers.remove(provider)

        for consumer in list(self.consumers):
            consumer.stop()
            self.consumers.remove(consumer)

    def add_provider(self, guid, any_keywords=None, all_keywords=None):
        '''
        Adds a provider to the capture, along with optional keywords.

        :param guid: The dict of the provider to add.
        :param any_keywords: list of any keywords to add for provider
        :param all_keywords: list of all keywords to add for provider
        :return: Does not return anything
        '''

        if any_keywords is None:
            any_keywords = []

        if all_keywords is None:
            all_keywords = []

        name, guid = list(guid.items())[0]
        any_bitmask = get_keywords_bitmask(guid, any_keywords)
        all_bitmask = get_keywords_bitmask(guid, all_keywords)
        self.guids[name] = (guid, any_bitmask, all_bitmask)


def get_keywords_bitmask(guid, keywords):
    """
    Queries available keywords of the provider and returns a bitmask of the associated values

    :param guid: The GUID of the ETW provider.
    :param keywords: List of keywords to resolve.
    :return Bitmask of the keyword flags ORed together
    """

    bitmask = 0
    if len(keywords) == 0:
        return bitmask

    # enumerate the keywords for the provider as well as the bitmask values
    provider_info = None
    providers_size = wt.ULONG(0)
    status = tdh.TdhEnumerateProviderFieldInformation(
        ct.byref(guid),
        tdh.EventKeywordInformation,
        provider_info,
        ct.byref(providers_size))

    if status == tdh.ERROR_INSUFFICIENT_BUFFER:

        provider_info = ct.cast((ct.c_char * providers_size.value)(), ct.POINTER(tdh.PROVIDER_FIELD_INFOARRAY))
        status = tdh.TdhEnumerateProviderFieldInformation(
            ct.byref(guid),
            tdh.EventKeywordInformation,
            provider_info,
            ct.byref(providers_size))

        if tdh.ERROR_SUCCESS != status and tdh.ERROR_NOT_FOUND != status:
            raise ct.WinError()

    if provider_info:
        field_info_array = ct.cast(provider_info.contents.FieldInfoArray, ct.POINTER(tdh.PROVIDER_FIELD_INFO))
        provider_keywords = {}
        for i in range(provider_info.contents.NumberOfElements):
            provider_keyword = rel_ptr_to_str(provider_info, field_info_array[i].NameOffset)
            provider_keywords[provider_keyword] = field_info_array[i].Value

        for keyword in keywords:
            if keyword in provider_keywords:
                bitmask |= provider_keywords[keyword]

    return bitmask
