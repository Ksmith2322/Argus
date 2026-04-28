<#
.SYNOPSIS
    Enable or disable AT&T BGW320 Wi-Fi radios (2.4GHz + 5GHz).

.DESCRIPTION
    Logs into the AT&T gateway using MD5(password+nonce) authentication,
    then toggles Wi-Fi radios. For Task Scheduler automation.

.EXAMPLE
    .\wifi_control.ps1 -Action enable    # Turn Wi-Fi ON (7am)
    .\wifi_control.ps1 -Action disable   # Turn Wi-Fi OFF (8pm)
#>

param(
    [Parameter(Mandatory=$true)]
    [ValidateSet("enable", "disable")]
    [string]$Action
)

$GatewayIP = "192.168.1.254"
$BaseURL = "https://$GatewayIP"
$LoginURL = "$BaseURL/cgi-bin/login.ha"
$WifiConfigURL = "$BaseURL/cgi-bin/wconfig.ha"
$Password = '3<>0#%88%3'
$LogFile = "C:\Argus\repo\ops\logs\wifi_control.log"

# Trust self-signed gateway cert
Add-Type @"
using System.Net;
using System.Security.Cryptography.X509Certificates;
public class TrustGateway : ICertificatePolicy {
    public bool CheckValidationResult(ServicePoint sp, X509Certificate cert,
        WebRequest req, int problem) { return true; }
}
"@
[System.Net.ServicePointManager]::CertificatePolicy = New-Object TrustGateway
[Net.ServicePointManager]::SecurityProtocol = 'Tls12,Tls11,Tls'

function Write-Log {
    param([string]$Message)
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "$ts  $Message"
    Write-Host $line
    $dir = Split-Path $LogFile -Parent
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    Add-Content -Path $LogFile -Value $line -ErrorAction SilentlyContinue
}

function Get-MD5Hash {
    param([string]$Text)
    $md5 = [System.Security.Cryptography.MD5]::Create()
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($Text)
    $hash = $md5.ComputeHash($bytes)
    return ($hash | ForEach-Object { $_.ToString("x2") }) -join ''
}

$WifiValue = if ($Action -eq "enable") { "on" } else { "off" }
Write-Log "=== Wi-Fi $Action started (setting radios to '$WifiValue') ==="

try {
    # Step 1: Get login page and extract nonce
    Write-Log "Fetching login page..."
    $loginPage = Invoke-WebRequest -Uri $WifiConfigURL -UseBasicParsing -SessionVariable session -TimeoutSec 15

    $nonce = ""
    if ($loginPage.Content -match 'name="nonce"\s+value="([^"]+)"') {
        $nonce = $Matches[1]
        Write-Log "Got nonce"
    } elseif ($loginPage.Content -match 'wl80211on') {
        Write-Log "Already authenticated, skipping login"
        $configPage = $loginPage
        $nonce = $null
    } else {
        Write-Log "ERROR: Unexpected login page format"
        exit 1
    }

    # Step 2: Login with MD5(password + nonce) - matches gateway's hashpwd() JS
    if ($nonce) {
        Write-Log "Computing hash and logging in..."
        $hashPassword = Get-MD5Hash -Text ($Password + $nonce)

        # Gateway expects: hashpassword = MD5(password+nonce), password = asterisks
        $stars = '*' * $Password.Length
        $loginBody = @{
            nonce        = $nonce
            password     = $stars
            hashpassword = $hashPassword
            Continue     = "Continue"
        }

        $loginResult = Invoke-WebRequest -Uri $LoginURL -Method POST -Body $loginBody `
            -WebSession $session -UseBasicParsing -TimeoutSec 15

        if ($loginResult.Content -match 'wl80211on') {
            Write-Log "Login successful - on config page"
            $configPage = $loginResult
        } elseif ($loginResult.Content -match 'Access Code Required') {
            Write-Log "ERROR: Login failed - authentication rejected"
            exit 1
        } else {
            # Try fetching config page with session cookies
            $configPage = Invoke-WebRequest -Uri $WifiConfigURL -WebSession $session `
                -UseBasicParsing -TimeoutSec 15
            if ($configPage.Content -match 'wl80211on') {
                Write-Log "Login redirected, now on config page"
            } else {
                Write-Log "ERROR: Could not reach config page after login"
                exit 1
            }
        }
    }

    # Step 3: Toggle 2.4GHz radio
    Write-Log "Setting 2.4GHz radio to '$WifiValue'..."
    $body24 = @{ wl80211on = $WifiValue }
    $result24 = Invoke-WebRequest -Uri $WifiConfigURL -Method POST -Body $body24 `
        -WebSession $session -UseBasicParsing -TimeoutSec 15

    if ($result24.Content -match "Changes saved") {
        Write-Log "2.4GHz radio: $WifiValue - Changes saved"
    } elseif ($result24.Content -match 'wl80211on') {
        Write-Log "2.4GHz radio: submitted (checking state...)"
        # Verify the select value
        if ($result24.Content -match "wl80211on.*selected.*$WifiValue") {
            Write-Log "2.4GHz confirmed: $WifiValue"
        }
    } else {
        Write-Log "WARNING: 2.4GHz response unclear"
    }

    Start-Sleep -Seconds 3

    # Step 4: Toggle 5GHz radio
    Write-Log "Setting 5GHz radio to '$WifiValue'..."
    $body5 = @{ wl80211on_5 = $WifiValue }
    $result5 = Invoke-WebRequest -Uri $WifiConfigURL -Method POST -Body $body5 `
        -WebSession $session -UseBasicParsing -TimeoutSec 15

    if ($result5.Content -match "Changes saved") {
        Write-Log "5GHz radio: $WifiValue - Changes saved"
    } elseif ($result5.Content -match 'wl80211on_5') {
        Write-Log "5GHz radio: submitted"
    } else {
        Write-Log "WARNING: 5GHz response unclear"
    }

    Write-Log "=== Wi-Fi $Action completed ==="
}
catch {
    Write-Log "ERROR: $($_.Exception.Message)"
    exit 1
}
