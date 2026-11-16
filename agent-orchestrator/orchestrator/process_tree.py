"""Windows 执行进程的 Job 生命周期，不改变任务的文件/网络权限。"""
import os
import subprocess
import time


PROCESS_SUSPEND_RESUME = 0x0800


def creation_flags():
    """受控进程先挂起，防止父进程绑定 Job 前已生成后代。"""
    return subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000004 if os.name == 'nt' else 0


def _resume_process_handle(process):
    """快速恢复路径：OpenProcess(PROCESS_SUSPEND_RESUME) + ntdll.NtResumeProcess。

    进程刚由 CREATE_SUSPENDED 创建、尚未执行任何用户代码，只有初始线程处于挂起状态；
    NtResumeProcess 一次性恢复进程全部线程，对本场景语义正确。它避免了
    _resume_initial_thread 每次启动都枚举全系统线程表（新建进程的线程几乎总在表尾，
    开销随系统线程数线性增长）。

    只在进程已归入 Job 之后调用。任何失败（OpenProcess 失败、ntdll 未导出该函数、
    NtResumeProcess 返回非 0 的 NTSTATUS、ctypes 调用异常）都返回 False，由调用方
    回退到 Toolhelp 扫描；NtResumeProcess 失败时不会留下"部分恢复"的可用进程，
    因为挂起状态来自 CREATE_SUSPENDED 的初始挂起计数，回退路径会再次恢复该线程。
    """
    import ctypes as C
    from ctypes import wintypes as W
    try:
        kernel = C.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
        kernel.OpenProcess.restype = W.HANDLE
        kernel.CloseHandle.argtypes = [W.HANDLE]
        kernel.CloseHandle.restype = W.BOOL
        ntdll = C.WinDLL('ntdll', use_last_error=True)
        # NtResumeProcess 是 NTAPI：返回 NTSTATUS，0 表示成功；非 0 一律回退。
        ntdll.NtResumeProcess.argtypes = [W.HANDLE]
        ntdll.NtResumeProcess.restype = C.c_long
        handle = kernel.OpenProcess(PROCESS_SUSPEND_RESUME, False, process.pid)
        if not handle:
            return False
        try:
            return ntdll.NtResumeProcess(handle) == 0
        finally:
            kernel.CloseHandle(handle)
    except Exception:
        return False


def _resume_initial_thread(process):
    """仅恢复本次挂起进程的初始线程；使用公开 Win32 线程枚举/恢复接口。"""
    import ctypes as C
    from ctypes import wintypes as W
    class ThreadEntry(C.Structure):
        _fields_=[('size',W.DWORD),('usage',W.DWORD),('thread_id',W.DWORD),
                  ('owner_pid',W.DWORD),('base_priority',W.LONG),('delta_priority',W.LONG),('flags',W.DWORD)]
    kernel=C.WinDLL('kernel32',use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes=[W.DWORD,W.DWORD]
    kernel.CreateToolhelp32Snapshot.restype=W.HANDLE
    kernel.Thread32First.argtypes=[W.HANDLE,C.POINTER(ThreadEntry)]
    kernel.Thread32First.restype=W.BOOL
    kernel.Thread32Next.argtypes=[W.HANDLE,C.POINTER(ThreadEntry)]
    kernel.Thread32Next.restype=W.BOOL
    kernel.OpenThread.argtypes=[W.DWORD,W.BOOL,W.DWORD]
    kernel.OpenThread.restype=W.HANDLE
    kernel.ResumeThread.argtypes=[W.HANDLE]
    kernel.ResumeThread.restype=W.DWORD
    kernel.CloseHandle.argtypes=[W.HANDLE]
    kernel.CloseHandle.restype=W.BOOL
    snapshot=kernel.CreateToolhelp32Snapshot(0x00000004,0)  # TH32CS_SNAPTHREAD
    if snapshot==C.c_void_p(-1).value:raise C.WinError(C.get_last_error())
    try:
        row=ThreadEntry();row.size=C.sizeof(row)
        found=kernel.Thread32First(snapshot,C.byref(row))
        while found:
            if row.owner_pid==process.pid:
                thread=kernel.OpenThread(0x0002,False,row.thread_id)  # THREAD_SUSPEND_RESUME
                if not thread:raise C.WinError(C.get_last_error())
                try:
                    if kernel.ResumeThread(thread)==0xFFFFFFFF:raise C.WinError(C.get_last_error())
                finally:kernel.CloseHandle(thread)
                return
            found=kernel.Thread32Next(snapshot,C.byref(row))
        raise OSError('未找到本次挂起进程的初始线程')
    finally:kernel.CloseHandle(snapshot)


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
        job=WindowsJob(process)
        process.__dict__['_workbench_job']=job
        try:
            if process.__dict__.get('_pipeline_start_suspended'):
                # 安全顺序：必须先归入 Job 才恢复，恢复出的后代才受 Job 约束。
                # 句柄级快速恢复失败时回退到全系统线程枚举（老路径，慢但可用）。
                if not _resume_process_handle(process):
                    _resume_initial_thread(process)
                process.__dict__['_pipeline_start_suspended']=False
        except BaseException:
            process.__dict__.pop('_workbench_job',None)
            job.close()  # KILL_ON_JOB_CLOSE，恢复失败不能留下仍挂起的执行进程。
            raise


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
