"""外部 benchmark Runner 运行时（独立于 API 进程的适配器包）。

本包只执行作业：实现 ``motto_contracts.external_job.ExternalJobAdapter``
协议的进程适配器、显式内置注册表与合成假 Runner。持久化、启动边界与
采集检查点由 ``motte_sdk.external_jobs`` 的应用层负责，adapter 不直接
修改 Run 表。
"""
