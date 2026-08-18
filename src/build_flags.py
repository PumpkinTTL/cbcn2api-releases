"""打包期构建标志（编译进二进制的常量，运行期不可修改）。

INTERNAL_BUILD=True 的包 = 内部豁免版：跳过全部授权校验。
build_internal.bat 打包前把本常量翻成 True，构建完自动还原（git checkout）。
正式发行包恒为 False —— 与 GW_DEV 开发豁免不同，豁免状态由编译期常量决定，
正式版用户设环境变量 / 改配置文件都无法触发。
"""
INTERNAL_BUILD = False
