# StudyMatrix 固定公网地址启动脚本
# 用法：双击本文件或在终端执行  .\start_tunnel.ps1
# 前提：Flask 服务已启动（python app.py，端口 5000）

$env:Path = [System.Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [System.Environment]::GetEnvironmentVariable('Path','User')

Write-Host "== StudyMatrix 固定域名隧道 ==" -ForegroundColor Green
Write-Host "公网地址: https://dealt-sworn-barrack.ngrok-free.dev" -ForegroundColor Cyan
Write-Host "本地服务: http://127.0.0.1:5000"
Write-Host "隧道状态页: http://127.0.0.1:4040"
Write-Host "按 Ctrl+C 关闭隧道" -ForegroundColor Yellow
Write-Host ""

ngrok http 5000 --url dealt-sworn-barrack.ngrok-free.dev
