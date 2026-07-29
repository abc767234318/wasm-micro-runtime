$ErrorActionPreference = "Stop"

$requiredCommands = @(
    @{ Name = "CMake"; Command = "cmake"; Hint = "Install CMake and add it to PATH." },
    @{ Name = "Ninja"; Command = "ninja"; Hint = "Install Ninja and add it to PATH." },
    @{ Name = "MSVC compiler"; Command = "cl"; Hint = "Run this script from Developer PowerShell for VS 2022." },
    @{ Name = "WABT wat2wasm"; Command = "wat2wasm"; Hint = "Install WABT and add wat2wasm.exe to PATH." },
    @{ Name = "VS Code CLI"; Command = "code"; Hint = "Install VS Code and enable the code command in PATH." }
)

$failed = $false

foreach ($item in $requiredCommands) {
    $resolved = Get-Command $item.Command -ErrorAction SilentlyContinue
    if ($null -eq $resolved) {
        Write-Host "[MISSING] $($item.Name): $($item.Hint)" -ForegroundColor Red
        $failed = $true
    }
    else {
        Write-Host "[OK] $($item.Name): $($resolved.Source)" -ForegroundColor Green
    }
}

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = (Resolve-Path (Join-Path $scriptRoot "../..")).Path
$requiredFiles = @(
    ".vscode/tasks.json",
    ".vscode/launch.json",
    ".vscode/c_cpp_properties.json",
    ".vscode/wasm/minimal.wat",
    "product-mini/platforms/windows/CMakeLists.txt"
)

foreach ($relativePath in $requiredFiles) {
    $fullPath = Join-Path $repoRoot $relativePath
    if (Test-Path $fullPath) {
        Write-Host "[OK] $relativePath" -ForegroundColor Green
    }
    else {
        Write-Host "[MISSING] $relativePath" -ForegroundColor Red
        $failed = $true
    }
}

if (Get-Command code -ErrorAction SilentlyContinue) {
    $extensions = @(& code --list-extensions 2>$null)
    if ($extensions -contains "ms-vscode.cpptools") {
        Write-Host "[OK] VS Code extension: ms-vscode.cpptools" -ForegroundColor Green
    }
    else {
        Write-Host "[MISSING] VS Code extension: ms-vscode.cpptools" -ForegroundColor Red
        Write-Host "          Install with: code --install-extension ms-vscode.cpptools"
        $failed = $true
    }
}

if ($failed) {
    Write-Host ""
    Write-Host "Windows WAMR study environment is not ready." -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "Windows WAMR study environment is ready." -ForegroundColor Green
Write-Host "Open the repository with: code ."
Write-Host "Then select: WAMR Classic [Windows]: Learning"
