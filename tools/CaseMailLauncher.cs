using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Management;
using System.Net;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Windows.Forms;

internal static class CaseMailLauncher
{
    private const int Port = 8000;
    private const string LocalBaseUrl = "http://127.0.0.1:8000";
    private static readonly Regex TunnelUrlRegex = new Regex("https://[-a-z0-9]+\\.trycloudflare\\.com", RegexOptions.IgnoreCase);
    private static readonly object LogLock = new object();

    private static Process _serverProcess;
    private static Process _cloudflaredProcess;
    private static StreamWriter _serverLog;
    private static StreamWriter _cloudflaredLog;
    private static string _tunnelBaseUrl = "";
    private static bool _tunnelHealthVerified;

    [STAThread]
    private static int Main(string[] args)
    {
        Console.OutputEncoding = Encoding.UTF8;
        Console.Title = "CaseMail IMAP Launcher";

        bool helpOnly = args.Length > 0 && (args[0] == "--help" || args[0] == "/?");
        if (helpOnly)
        {
            PrintHelp();
            return 0;
        }

        string repoRoot = FindRepoRoot();
        string cacheDir = Path.Combine(repoRoot, ".cache");
        Directory.CreateDirectory(cacheDir);

        string envPath = Path.Combine(repoRoot, ".env");
        if (!File.Exists(envPath))
        {
            Console.WriteLine("Could not find .env next to this repository.");
            Console.WriteLine("Open the project folder and configure CaseMail once before using the launcher.");
            Pause();
            return 2;
        }

        Dictionary<string, string> env = ReadEnv(envPath);
        string python = FindExecutable("python.exe", null);
        string cloudflared = FindCloudflared();

        bool checkOnly = args.Length > 0 && args[0] == "--check";
        if (checkOnly)
        {
            string configuredToken;
            env.TryGetValue("CASEMAIL_ACCESS_TOKEN", out configuredToken);
            Console.WriteLine("CaseMail launcher check");
            Console.WriteLine("Project: " + repoRoot);
            Console.WriteLine("Python: " + (string.IsNullOrWhiteSpace(python) ? "NOT FOUND" : python));
            Console.WriteLine("cloudflared: " + (string.IsNullOrWhiteSpace(cloudflared) ? "NOT FOUND" : cloudflared));
            Console.WriteLine("Access token configured: " + (!string.IsNullOrWhiteSpace(configuredToken) ? "yes" : "no"));
            Console.WriteLine("Local server currently running: " + (HttpGetOk(LocalBaseUrl + "/healthz", 3000) ? "yes" : "no"));
            return string.IsNullOrWhiteSpace(python) || string.IsNullOrWhiteSpace(cloudflared) ? 2 : 0;
        }

        string token = EnsureAccessToken(envPath, env);

        if (string.IsNullOrWhiteSpace(python))
        {
            Console.WriteLine("Python was not found in PATH.");
            Console.WriteLine("Install Python or add it to PATH, then run this launcher again.");
            Pause();
            return 2;
        }

        if (string.IsNullOrWhiteSpace(cloudflared))
        {
            Console.WriteLine("cloudflared was not found.");
            Console.WriteLine("Install it with: winget install --id Cloudflare.cloudflared");
            Pause();
            return 2;
        }

        Console.CancelKeyPress += delegate(object sender, ConsoleCancelEventArgs eventArgs)
        {
            eventArgs.Cancel = true;
            StopStartedProcesses();
            Environment.Exit(0);
        };

        try
        {
            Console.WriteLine("CaseMail IMAP Launcher");
            Console.WriteLine("======================");
            Console.WriteLine("Project: " + repoRoot);
            Console.WriteLine();

            StartServerIfNeeded(repoRoot, cacheDir, python);
            StartFreshTunnel(repoRoot, cacheDir, cloudflared);

            string mcpUrl = _tunnelBaseUrl.TrimEnd('/') + "/mcp/";
            string fallbackUrl = _tunnelBaseUrl.TrimEnd('/') + "/casemail/" + token;
            string adminUrl = LocalBaseUrl + "/admin?access_token=" + token;
            string connectionBlock =
                "CaseMail IMAP ChatGPT Developer Mode" + Environment.NewLine +
                "URL: " + mcpUrl + Environment.NewLine +
                "API key: " + token + Environment.NewLine +
                "Fallback URL: " + fallbackUrl + Environment.NewLine +
                "Local admin: " + adminUrl;

            File.WriteAllText(Path.Combine(cacheDir, "chatgpt-connection.txt"), connectionBlock, Encoding.UTF8);
            TryCopyToClipboard(connectionBlock);

            Console.WriteLine();
            Console.WriteLine(_tunnelHealthVerified ? "Ready." : "Ready, but the Cloudflare URL could not be verified yet.");
            if (!_tunnelHealthVerified)
            {
                Console.WriteLine("If ChatGPT cannot connect immediately, wait a minute or run the launcher again to get a different quick-tunnel URL.");
            }
            Console.WriteLine();
            Console.WriteLine("ChatGPT connector URL:");
            Console.WriteLine(mcpUrl);
            Console.WriteLine();
            Console.WriteLine("API key:");
            Console.WriteLine(token);
            Console.WriteLine();
            Console.WriteLine("Fallback URL, if API-key auth acts weird:");
            Console.WriteLine(fallbackUrl);
            Console.WriteLine();
            Console.WriteLine("Local admin:");
            Console.WriteLine(adminUrl);
            Console.WriteLine();
            Console.WriteLine("I copied the connection block to the clipboard and saved it here:");
            Console.WriteLine(Path.Combine(cacheDir, "chatgpt-connection.txt"));
            Console.WriteLine();
            Console.WriteLine("Keep this window open while using ChatGPT.");
            Console.WriteLine("Press Enter here when you want to stop the tunnel and any server started by this launcher.");
            Console.ReadLine();
            return 0;
        }
        catch (Exception ex)
        {
            Console.WriteLine();
            Console.WriteLine("Launcher error:");
            Console.WriteLine(ex.Message);
            Console.WriteLine();
            Console.WriteLine("Logs are in: " + cacheDir);
            Pause();
            return 1;
        }
        finally
        {
            StopStartedProcesses();
            CloseLogs();
        }
    }

    private static void PrintHelp()
    {
        Console.WriteLine("CaseMail IMAP Launcher");
        Console.WriteLine("Starts the local MCP server, starts a fresh Cloudflare quick tunnel,");
        Console.WriteLine("then prints the ChatGPT URL and API key from .env.");
        Console.WriteLine();
        Console.WriteLine("Options:");
        Console.WriteLine("  --check    Validate local dependencies without starting or stopping anything.");
    }

    private static string FindRepoRoot()
    {
        string dir = AppDomain.CurrentDomain.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar);
        for (int i = 0; i < 5 && !string.IsNullOrWhiteSpace(dir); i++)
        {
            if (File.Exists(Path.Combine(dir, "pyproject.toml")) && Directory.Exists(Path.Combine(dir, "src")))
            {
                return dir;
            }
            dir = Directory.GetParent(dir) == null ? "" : Directory.GetParent(dir).FullName;
        }
        return Directory.GetCurrentDirectory();
    }

    private static Dictionary<string, string> ReadEnv(string envPath)
    {
        Dictionary<string, string> values = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        foreach (string rawLine in File.ReadAllLines(envPath, Encoding.UTF8))
        {
            string line = rawLine.Trim();
            if (line.Length == 0 || line.StartsWith("#") || !line.Contains("="))
            {
                continue;
            }
            int split = line.IndexOf('=');
            string key = line.Substring(0, split).Trim();
            string value = line.Substring(split + 1).Trim().Trim('"');
            values[key] = value;
        }
        return values;
    }

    private static string EnsureAccessToken(string envPath, Dictionary<string, string> env)
    {
        string token;
        env.TryGetValue("CASEMAIL_ACCESS_TOKEN", out token);
        if (!string.IsNullOrWhiteSpace(token))
        {
            return token.Trim();
        }

        token = GenerateToken();
        using (StreamWriter writer = File.AppendText(envPath))
        {
            writer.WriteLine();
            writer.WriteLine("CASEMAIL_ACCESS_TOKEN=" + token);
            writer.WriteLine("CASEMAIL_AUTH_REQUIRED=true");
        }
        Console.WriteLine("Generated a new CASEMAIL_ACCESS_TOKEN in .env.");
        return token;
    }

    private static string GenerateToken()
    {
        byte[] bytes = new byte[32];
        using (RandomNumberGenerator rng = RandomNumberGenerator.Create())
        {
            rng.GetBytes(bytes);
        }
        return Convert.ToBase64String(bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_');
    }

    private static string FindCloudflared()
    {
        string found = FindExecutable("cloudflared.exe", null);
        if (!string.IsNullOrWhiteSpace(found))
        {
            return found;
        }

        string[] candidates =
        {
            @"C:\Program Files (x86)\cloudflared\cloudflared.exe",
            @"C:\Program Files\cloudflared\cloudflared.exe"
        };
        foreach (string candidate in candidates)
        {
            if (File.Exists(candidate))
            {
                return candidate;
            }
        }
        return "";
    }

    private static string FindExecutable(string name, string fallback)
    {
        string path = Environment.GetEnvironmentVariable("PATH") ?? "";
        foreach (string dir in path.Split(Path.PathSeparator))
        {
            try
            {
                string candidate = Path.Combine(dir.Trim(), name);
                if (File.Exists(candidate))
                {
                    return candidate;
                }
            }
            catch
            {
                // Ignore malformed PATH entries.
            }
        }
        return fallback ?? "";
    }

    private static void StartServerIfNeeded(string repoRoot, string cacheDir, string python)
    {
        Console.WriteLine("Checking local MCP server...");
        if (HttpGetOk(LocalBaseUrl + "/healthz", 3000))
        {
            Console.WriteLine("Local MCP server is already running.");
            return;
        }

        Console.WriteLine("Starting local MCP server...");
        _serverLog = new StreamWriter(Path.Combine(cacheDir, "launcher-uvicorn.log"), false, Encoding.UTF8);
        ProcessStartInfo startInfo = new ProcessStartInfo();
        startInfo.FileName = python;
        startInfo.Arguments = "-m uvicorn casemail_imap_mcp.server:create_app --factory --host 127.0.0.1 --port " + Port;
        startInfo.WorkingDirectory = repoRoot;
        startInfo.UseShellExecute = false;
        startInfo.CreateNoWindow = true;
        startInfo.RedirectStandardOutput = true;
        startInfo.RedirectStandardError = true;
        startInfo.EnvironmentVariables["PYTHONPATH"] = "src";

        _serverProcess = Process.Start(startInfo);
        AttachLogReaders(_serverProcess, _serverLog, null);

        if (!WaitForHttp(LocalBaseUrl + "/healthz", 30000))
        {
            throw new InvalidOperationException("The local MCP server did not become ready. See .cache\\launcher-uvicorn.log.");
        }
        Console.WriteLine("Local MCP server is ready.");
    }

    private static void StartFreshTunnel(string repoRoot, string cacheDir, string cloudflared)
    {
        Console.WriteLine("Stopping old CaseMail cloudflared tunnels, if any...");
        StopExistingCaseMailCloudflared();

        Console.WriteLine("Starting a fresh Cloudflare quick tunnel...");
        _cloudflaredLog = new StreamWriter(Path.Combine(cacheDir, "launcher-cloudflared.log"), false, Encoding.UTF8);
        _tunnelHealthVerified = false;

        const int maxAttempts = 2;
        for (int attempt = 1; attempt <= maxAttempts; attempt++)
        {
            _tunnelBaseUrl = "";
            if (attempt > 1)
            {
                Console.WriteLine("Retrying Cloudflare quick tunnel, attempt " + attempt + " of " + maxAttempts + "...");
            }

            ProcessStartInfo startInfo = new ProcessStartInfo();
            startInfo.FileName = cloudflared;
            startInfo.Arguments = "tunnel --protocol http2 --url " + LocalBaseUrl;
            startInfo.WorkingDirectory = repoRoot;
            startInfo.UseShellExecute = false;
            startInfo.CreateNoWindow = true;
            startInfo.RedirectStandardOutput = true;
            startInfo.RedirectStandardError = true;

            _cloudflaredProcess = Process.Start(startInfo);
            AttachLogReaders(_cloudflaredProcess, _cloudflaredLog, delegate(string line)
            {
                Match match = TunnelUrlRegex.Match(line);
                if (match.Success && _tunnelBaseUrl != match.Value)
                {
                    _tunnelBaseUrl = match.Value;
                    Console.WriteLine("Cloudflare tunnel URL received:");
                    Console.WriteLine(_tunnelBaseUrl);
                    Console.WriteLine("Waiting for the tunnel DNS and health check...");
                }
            });

            DateTime deadline = DateTime.UtcNow.AddSeconds(30);
            DateTime nextProgress = DateTime.UtcNow.AddSeconds(5);
            while (DateTime.UtcNow < deadline)
            {
                if (!string.IsNullOrWhiteSpace(_tunnelBaseUrl) && HttpGetOk(_tunnelBaseUrl.TrimEnd('/') + "/healthz", 10000))
                {
                    _tunnelHealthVerified = true;
                    Console.WriteLine("Cloudflare tunnel is ready: " + _tunnelBaseUrl);
                    return;
                }
                if (DateTime.UtcNow >= nextProgress)
                {
                    Console.WriteLine(string.IsNullOrWhiteSpace(_tunnelBaseUrl)
                        ? "Still waiting for Cloudflare to assign a tunnel URL..."
                        : "Tunnel URL assigned, still waiting for it to become reachable...");
                    nextProgress = DateTime.UtcNow.AddSeconds(10);
                }
                Thread.Sleep(1000);
            }

            if (!string.IsNullOrWhiteSpace(_tunnelBaseUrl))
            {
                Console.WriteLine("Cloudflare assigned a URL, but it did not become reachable in time.");
                if (attempt == maxAttempts)
                {
                    Console.WriteLine("Keeping the final tunnel alive and showing the URL anyway.");
                    return;
                }
            }
            else
            {
                Console.WriteLine("Cloudflare did not assign a URL in time.");
            }

            if (_cloudflaredProcess != null && !_cloudflaredProcess.HasExited)
            {
                TryKill(_cloudflaredProcess);
            }
            _cloudflaredProcess = null;
            Thread.Sleep(2000);
        }

        throw new InvalidOperationException("Cloudflare did not return a working tunnel URL. See .cache\\launcher-cloudflared.log.");
    }

    private static void AttachLogReaders(Process process, StreamWriter log, Action<string> onLine)
    {
        DataReceivedEventHandler handler = delegate(object sender, DataReceivedEventArgs args)
        {
            if (args.Data == null)
            {
                return;
            }
            lock (LogLock)
            {
                log.WriteLine(DateTime.Now.ToString("s") + " " + args.Data);
                log.Flush();
            }
            if (onLine != null)
            {
                onLine(args.Data);
            }
        };
        process.OutputDataReceived += handler;
        process.ErrorDataReceived += handler;
        process.BeginOutputReadLine();
        process.BeginErrorReadLine();
    }

    private static void StopExistingCaseMailCloudflared()
    {
        try
        {
            using (ManagementObjectSearcher searcher = new ManagementObjectSearcher("SELECT ProcessId, CommandLine FROM Win32_Process WHERE Name='cloudflared.exe'"))
            {
                foreach (ManagementObject process in searcher.Get())
                {
                    string commandLine = Convert.ToString(process["CommandLine"] ?? "");
                    if (commandLine.IndexOf("tunnel", StringComparison.OrdinalIgnoreCase) >= 0 &&
                        commandLine.IndexOf(LocalBaseUrl, StringComparison.OrdinalIgnoreCase) >= 0)
                    {
                        int pid = Convert.ToInt32(process["ProcessId"]);
                        try
                        {
                            Process.GetProcessById(pid).Kill();
                        }
                        catch
                        {
                            // Best effort cleanup only.
                        }
                    }
                }
            }
        }
        catch
        {
            // WMI can fail under restrictive environments; starting a new
            // tunnel is still safe, just less tidy.
        }
    }

    private static bool WaitForHttp(string url, int timeoutMs)
    {
        DateTime deadline = DateTime.UtcNow.AddMilliseconds(timeoutMs);
        while (DateTime.UtcNow < deadline)
        {
            if (HttpGetOk(url, 3000))
            {
                return true;
            }
            Thread.Sleep(500);
        }
        return false;
    }

    private static bool HttpGetOk(string url, int timeoutMs)
    {
        try
        {
            HttpWebRequest request = (HttpWebRequest)WebRequest.Create(url);
            request.Timeout = timeoutMs;
            request.ReadWriteTimeout = timeoutMs;
            using (HttpWebResponse response = (HttpWebResponse)request.GetResponse())
            {
                return response.StatusCode == HttpStatusCode.OK;
            }
        }
        catch
        {
            return false;
        }
    }

    private static void TryCopyToClipboard(string text)
    {
        try
        {
            Clipboard.SetText(text);
        }
        catch
        {
            Console.WriteLine("Could not copy to clipboard automatically.");
        }
    }

    private static void StopStartedProcesses()
    {
        if (_cloudflaredProcess != null && !_cloudflaredProcess.HasExited)
        {
            TryKill(_cloudflaredProcess);
        }
        if (_serverProcess != null && !_serverProcess.HasExited)
        {
            TryKill(_serverProcess);
        }
    }

    private static void TryKill(Process process)
    {
        try
        {
            process.Kill();
            process.WaitForExit(5000);
        }
        catch
        {
            // Best effort shutdown.
        }
    }

    private static void CloseLogs()
    {
        if (_serverLog != null)
        {
            _serverLog.Dispose();
        }
        if (_cloudflaredLog != null)
        {
            _cloudflaredLog.Dispose();
        }
    }

    private static void Pause()
    {
        Console.WriteLine();
        Console.WriteLine("Press Enter to close.");
        Console.ReadLine();
    }
}
