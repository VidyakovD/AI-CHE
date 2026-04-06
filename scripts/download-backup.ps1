# Скачать последнюю копию БД с сервера
$server = "root@194.104.9.219:/root/AI-CHE/backups/"
$localDir = ".\backups"

if (-not (Test-Path $localDir)) {
    mkdir $localDir
}

scp -r $server $localDir

Write-Host "Backups downloaded to $localDir"
