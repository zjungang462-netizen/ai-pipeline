"""Windows 执行进程的 Job 生命周期，不改变任务的文件/网络权限。"""
import os
import time


class WindowsJob:
    def __init__(self, process):
        import ctypes as C
        from ctypes import wintypes as W
        self.C = C
        class Limits(C.Structure):
            _fields_ = [('process_time', C.c_int64), ('job_time', C.c_int64), ('flags', W.DWORD),
                        ('minimum', C.c_size_t), ('maximum', C.c_size_t), ('active', W.DWORD),
                        ('affinity', C.c_size_t), ('priority', W.DWORD), ('scheduling', W.DWORD)]
        class Counters(C.Structure):
            _fields_ = [('values', C.c_uint64 * 6)]
        class Extended(C.Structure):
            _fields_ = [('limits', Limits), ('io', Counters), ('process_memory', C.c_size_t),
                        ('job_memory', C.c_size_t), ('peak_process', C.c_size_t), ('peak_job', C.c_size_t)]
        class Accounting(C.Structure):
            _fields_ = [('times', C.c_int64 * 4), ('faults', W.DWORD), ('total', W.DWORD),
                        ('active', W.DWORD), ('terminated', W.DWORD)]
        self.Accounting = Accounting
        kernel = C.WinDLL('kernel32', use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [C.c_void_p, W.LPCWSTR]
        kernel.CreateJobObjectW.restype = W.HANDLE
        kernel.SetInformationJobObject.argtypes = [W.HANDLE, C.c_int, C.c_void_p, W.DWORD]
        kernel.SetInformationJobObject.restype = W.BOOL
        kernel.AssignProcessToJobObject.argtypes = [W.HANDLE, W.HANDLE]
        kernel.AssignProcessToJobObject.restype = W.BOOL
        kernel.TerminateJobObject.argtypes = [W.HANDLE, W.UINT]
        kernel.TerminateJobObject.restype = W.BOOL
        kernel.QueryInformationJobObject.argtypes = [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.c_void_p]
        kernel.QueryInformationJobObject.restype = W.BOOL
        kernel.CloseHandle.argtypes = [W.HANDLE]
        kernel.CloseHandle.restype = W.BOOL
        self.kernel = kernel
        self.handle = kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise C.WinError(C.get_last_error())
        limits = Extended()
        limits.limits.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if (not kernel.SetInformationJobObject(self.handle, 9, C.byref(limits), C.sizeof(limits))
                or not kernel.AssignProcessToJobObject(self.handle, W.HANDLE(int(process._handle)))):
            code = C.get_last_error()
            self.close()
            raise C.WinError(code)

    def terminate(self):
        if not self.handle:
            return
        if not self.kernel.TerminateJobObject(self.handle, 1):
            raise self.C.WinError(self.C.get_last_error())
        end = time.monotonic() + 5
        while time.monotonic() < end:
            data = self.Accounting()
            if not self.kernel.QueryInformationJobObject(self.handle, 1, self.C.byref(data), self.C.sizeof(data), None):
                raise self.C.WinError(self.C.get_last_error())
            if data.active == 0:
                return
            time.sleep(.02)
        raise RuntimeError('执行进程树尚未完全退出，不能确认取消')

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def attach(process):
    if os.name == 'nt':
        process.__dict__['_workbench_job'] = WindowsJob(process)


def terminate(process):
    job = process.__dict__.get('_workbench_job')
    if job:
        job.terminate()
        return True
    return False


def close(process):
    job = process.__dict__.pop('_workbench_job', None)
    if job:
        job.close()
