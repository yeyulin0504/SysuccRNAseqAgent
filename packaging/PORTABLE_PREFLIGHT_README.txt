院内 HPC 只读预检便携包（Windows x64）
========================================

包内不含 FASTQ、样本名、read ID、患者资料、服务器密码、API key 或模型配置。
preflight_project.json 只包含连接目标、工作目录、工具开关和参考文件路径；它仍属于院内基础设施信息，只能发送到你信任并获授权使用的电脑。

使用步骤
--------
1. 把整个 ZIP 复制到另一台 Windows x64 电脑并完整解压，不能直接在 ZIP 里运行。
2. 连接医院 VPN。
3. 如果该电脑从未登录过服务器，先用医院提供的 SSH 主机指纹完成首次信任：
   - 在 PowerShell 中运行你平时使用的 ssh 用户名@服务器地址；
   - 将显示的指纹与医院管理员提供的指纹逐字核对；
   - 指纹不一致时立即停止，不能删除 known_hosts 或跳过校验。
4. 双击 RUN_PREFLIGHT.bat。
5. 终端提示输入 SSH 密码时直接输入并回车。输入过程不显示字符或星号，这是正常的。
6. 完成后得到 server_preflight.json。只需把这个 JSON 带回原电脑，不要带回密码。

安全边界
--------
- Agent 发出的固定探针不读取本地 FASTQ，不上传/下载数据，不创建远程项目文件，不提交 Slurm/PBS 任务，也不执行 init_commands。
- SSH 服务仍可能按医院制度记录正常登录、lastlog 和安全审计日志。
- 密码只交给当前短生命周期进程，代码不主动写入文件、环境变量、命令行或报告；Python 字符串不能提供物理内存取证级擦除保证。
- 未知或变化的 SSH 主机指纹会被拒绝，不采用自动信任。
- 本程序为未签名的内部测试构建。运行前应核对 SHA256SUMS.txt；哈希不一致时不要运行。

文件说明
--------
- RUN_PREFLIGHT.bat：双击运行完整预检。
- SELF_TEST_ONLY.bat：只检查本地便携运行时，不连接服务器。
- readonly_hpc_preflight.exe：便携执行程序。
- preflight_project.json：不含样本/FASTQ 的最小预检配置。
- server_preflight.json：成功连接后生成的脱敏报告。
- README_FIRST.txt：本说明。
- SHA256SUMS.txt：交付文件哈希。
- licenses/：Python 和第三方依赖许可证。
