# Reinstala automaxia-utils em todas as venvs que ja tem o pacote instalado.
#
# Uso:
#   .\reinstall_in_consumers.ps1                          # busca em D:\Automaxia\clientes
#   .\reinstall_in_consumers.ps1 -Root C:\outro           # busca em outra raiz
#   .\reinstall_in_consumers.ps1 -DryRun                  # so lista, nao instala
#   .\reinstall_in_consumers.ps1 -Exclude 'old','legacy'  # pula venvs cujo caminho contem qualquer um dos termos
#
# Como funciona:
#   - Varre <Root> procurando diretorios `site-packages\automaxia_utils`.
#   - Para cada um, sobe ate achar o `pip.exe` da venv (Scripts\pip.exe).
#   - Roda `pip install --force-reinstall --no-deps` apontando para esta source.
#
# Quando rodar:
#   - Apos qualquer mudanca no codigo deste repositorio que precise se
#     propagar aos produtos consumidores (admincenter/datachatai/agente_db/etc).
#   - Para deploy em outras maquinas, use o caminho Git padrao (vide README).

param(
    [string]$Root = "D:\Automaxia\clientes",
    [string[]]$Exclude = @(),
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$source = $PSScriptRoot

Write-Host ""
Write-Host "automaxia-utils source : $source" -ForegroundColor Cyan
Write-Host "Procurando consumidores em: $Root" -ForegroundColor Cyan
Write-Host ""

if (-not (Test-Path $Root)) {
    Write-Host "Raiz nao existe: $Root" -ForegroundColor Red
    exit 1
}

# Encontra todas as instalacoes do pacote
$installs = Get-ChildItem -Path $Root -Recurse -Directory `
    -Filter "automaxia_utils" -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -match "site-packages\\automaxia_utils$" }

if ($installs.Count -eq 0) {
    Write-Host "Nenhuma instalacao encontrada." -ForegroundColor Yellow
    exit 0
}

Write-Host "Encontradas $($installs.Count) instalacao(coes):" -ForegroundColor Green
Write-Host ""

$resultados = @()

foreach ($inst in $installs) {
    # Sobe de .../venv/Lib/site-packages/automaxia_utils ate .../venv
    $venv = Split-Path (Split-Path (Split-Path $inst.FullName -Parent) -Parent) -Parent
    $pip = Join-Path $venv "Scripts\pip.exe"

    # Diretorio do produto = pai da venv (geralmente)
    $produto = Split-Path $venv -Parent
    $produtoNome = Split-Path $produto -Leaf

    Write-Host "[$produtoNome]" -ForegroundColor White
    Write-Host "  venv: $venv"

    # Pula a propria venv deste repo — instalar nela e' redundante
    if ($venv.StartsWith($source, [StringComparison]::OrdinalIgnoreCase)) {
        Write-Host "  -> pulando (venv do proprio source)" -ForegroundColor DarkGray
        Write-Host ""
        continue
    }

    # Pula venvs cujo caminho casa com qualquer termo de -Exclude
    $skip = $false
    foreach ($term in $Exclude) {
        if ($venv -like "*$term*") {
            Write-Host "  -> pulando (-Exclude '$term')" -ForegroundColor DarkGray
            $skip = $true
            break
        }
    }
    if ($skip) { Write-Host ""; continue }

    if (-not (Test-Path $pip)) {
        Write-Host "  -> pip nao encontrado em $pip" -ForegroundColor Yellow
        Write-Host ""
        continue
    }

    if ($DryRun) {
        Write-Host "  -> [dry-run] $pip install --force-reinstall --no-deps `"$source`"" -ForegroundColor DarkGray
        Write-Host ""
        continue
    }

    Write-Host "  -> reinstalando..." -ForegroundColor Cyan
    & $pip install --force-reinstall --no-deps $source 2>&1 |
        Select-String -Pattern "Successfully|ERROR|error:" |
        ForEach-Object { Write-Host "     $_" }
    if ($LASTEXITCODE -eq 0) {
        $resultados += [pscustomobject]@{ Produto = $produtoNome; Status = "OK" }
    } else {
        $resultados += [pscustomobject]@{ Produto = $produtoNome; Status = "FAIL ($LASTEXITCODE)" }
    }
    Write-Host ""
}

if (-not $DryRun -and $resultados.Count -gt 0) {
    Write-Host "Resumo:" -ForegroundColor Cyan
    $resultados | Format-Table -AutoSize
}
