using System;
using System.IO;
using System.Text;
using System.Text.RegularExpressions;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Reflection;
using Microsoft.Win32;

namespace OsuMapLauncher
{
    static class Program
    {
        [DllImport("user32.dll")]
        static extern short GetAsyncKeyState(int vKey);

        [DllImport("user32.dll")]
        static extern IntPtr GetForegroundWindow();

        [DllImport("user32.dll", SetLastError = true, CharSet = CharSet.Auto)]
        static extern int GetWindowText(IntPtr hWnd, StringBuilder lpString, int nMaxCount);

        [DllImport("user32.dll")]
        static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);

        [DllImport("kernel32.dll")]
        static extern IntPtr OpenProcess(uint dwDesiredAccess, bool bInheritHandle, uint dwProcessId);

        [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Auto)]
        static extern bool QueryFullProcessImageName(IntPtr hProcess, int flags, StringBuilder lpExeName, ref uint lpdwSize);

        [DllImport("kernel32.dll")]
        static extern bool CloseHandle(IntPtr hObject);

        [DllImport("user32.dll")]
        static extern bool AllowSetForegroundWindow(int dwProcessId);

        [DllImport("kernel32.dll")]
        static extern bool AttachConsole(int dwProcessId);

        private const int ATTACH_PARENT_PROCESS = -1;
        private const int ASFW_ANY = -1;
        private const int VK_SHIFT = 0x10;
        private const int VK_LSHIFT = 0xA0;
        private const int VK_RSHIFT = 0xA1;

        private static readonly Regex BeatmapRegex = new Regex(
            @"^https?://(?:osu\.ppy\.sh|old\.ppy\.sh)/(?:beatmapsets|beatmaps|b|s|d)/.+$",
            RegexOptions.IgnoreCase | RegexOptions.Compiled
        );

        private static readonly string LogFile = Path.Combine(Path.GetTempPath(), "osu_map_handler_log.txt");

        private static void Log(string msg)
        {
            try
            {
                string line = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " " + msg + Environment.NewLine;
                File.AppendAllText(LogFile, line, Encoding.UTF8);
            }
            catch { }
        }

        private static bool IsShiftPhysicallyDown()
        {
            return ((GetAsyncKeyState(VK_SHIFT) & 0x8000) != 0)
                || ((GetAsyncKeyState(VK_LSHIFT) & 0x8000) != 0)
                || ((GetAsyncKeyState(VK_RSHIFT) & 0x8000) != 0)
                || ((GetAsyncKeyState(VK_SHIFT) & 0x0001) != 0)
                || ((GetAsyncKeyState(VK_LSHIFT) & 0x0001) != 0)
                || ((GetAsyncKeyState(VK_RSHIFT) & 0x0001) != 0);
        }

        private static void GetForegroundInfo(out string procName, out string windowTitle)
        {
            procName = null;
            windowTitle = null;
            try
            {
                IntPtr hwnd = GetForegroundWindow();
                if (hwnd == IntPtr.Zero) return;

                var sbTitle = new StringBuilder(512);
                if (GetWindowText(hwnd, sbTitle, 512) > 0)
                    windowTitle = sbTitle.ToString();

                uint pid;
                GetWindowThreadProcessId(hwnd, out pid);
                if (pid != 0)
                {
                    IntPtr hProc = OpenProcess(0x1000 /* PROCESS_QUERY_LIMITED_INFORMATION */, false, pid);
                    if (hProc != IntPtr.Zero)
                    {
                        try
                        {
                            var sbPath = new StringBuilder(1024);
                            uint size = (uint)sbPath.Capacity;
                            if (QueryFullProcessImageName(hProc, 0, sbPath, ref size))
                            {
                                procName = Path.GetFileName(sbPath.ToString()).ToLowerInvariant();
                            }
                        }
                        finally
                        {
                            CloseHandle(hProc);
                        }
                    }
                }
            }
            catch { }
        }

        private static bool IsOsuForeground(string procName, string windowTitle)
        {
            if (string.IsNullOrEmpty(procName)) return false;
            if (procName == "osu!.exe" || procName == "osu.exe") return true;
            if (procName == "discord.exe" || procName == "chrome.exe" || procName == "firefox.exe" ||
                procName == "msedge.exe" || procName == "zen.exe" || procName == "brave.exe" ||
                procName == "devenv.exe" || procName == "code.exe")
            {
                return false;
            }
            if (!string.IsNullOrEmpty(windowTitle) &&
                (windowTitle.StartsWith("osu!", StringComparison.OrdinalIgnoreCase) ||
                 windowTitle.EndsWith("osu!", StringComparison.OrdinalIgnoreCase)))
            {
                return true;
            }
            return false;
        }

        private static string GetSavedBrowserCmd()
        {
            try
            {
                using (var key = Registry.CurrentUser.OpenSubKey(@"SOFTWARE\OsuMapHandler\Config"))
                {
                    if (key != null)
                    {
                        object val = key.GetValue("FallbackBrowserCmd");
                        if (val != null) return val.ToString();
                    }
                }
            }
            catch { }
            return null;
        }

        private static string ExtractExeFromCmd(string cmd)
        {
            if (string.IsNullOrEmpty(cmd)) return null;
            cmd = cmd.Trim();
            if (cmd.StartsWith("\""))
            {
                int end = cmd.IndexOf('\"', 1);
                if (end > 1) return cmd.Substring(1, end - 1);
            }
            int sp = cmd.IndexOf(' ');
            return sp > 0 ? cmd.Substring(0, sp).Trim('\"') : cmd.Trim('\"');
        }

        private static string ResolveBrowserExe(out string rawCmd)
        {
            rawCmd = GetSavedBrowserCmd();
            if (!string.IsNullOrEmpty(rawCmd))
            {
                string exe = ExtractExeFromCmd(rawCmd);
                if (!string.IsNullOrEmpty(exe) && File.Exists(exe))
                    return exe;
            }

            string[] commonPaths = {
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), @"Mozilla Firefox\firefox.exe"),
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86), @"Mozilla Firefox\firefox.exe"),
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), @"Zen Browser\zen.exe"),
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), @"Zen Browser\zen.exe"),
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), @"Floorp\floorp.exe"),
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), @"Waterfox\waterfox.exe"),
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), @"Google\Chrome\Application\chrome.exe"),
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86), @"Google\Chrome\Application\chrome.exe"),
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), @"Google\Chrome\Application\chrome.exe"),
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), @"BraveSoftware\Brave-Browser\Application\brave.exe"),
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86), @"BraveSoftware\Brave-Browser\Application\brave.exe"),
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), @"BraveSoftware\Brave-Browser\Application\brave.exe"),
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), @"Programs\Opera GX\launcher.exe"),
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), @"Programs\Opera\launcher.exe"),
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86), @"Microsoft\Edge\Application\msedge.exe"),
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), @"Microsoft\Edge\Application\msedge.exe"),
            };

            foreach (var p in commonPaths)
            {
                if (File.Exists(p)) return p;
            }
            return null;
        }

        private static void OpenInBrowser(string url)
        {
            try
            {
                AllowSetForegroundWindow(ASFW_ANY);

                string rawCmd;
                string browserExe = ResolveBrowserExe(out rawCmd);
                string cleanUrl = (url ?? "").Trim('\"', '\'', ' ');

                ProcessStartInfo psi;
                if (!string.IsNullOrEmpty(browserExe) && File.Exists(browserExe))
                {
                    // Direct process launch without quotes avoids Chromium/Brave interpreting URLs as search queries
                    psi = new ProcessStartInfo
                    {
                        FileName = browserExe,
                        Arguments = cleanUrl,
                        UseShellExecute = false
                    };
                }
                else
                {
                    psi = new ProcessStartInfo
                    {
                        FileName = cleanUrl,
                        UseShellExecute = true
                    };
                }

                Process.Start(psi);
            }
            catch (Exception ex)
            {
                Log("Failed to open browser: " + ex.Message);
            }
        }

        private static string GetEnginePath()
        {
            string dir = AppDomain.CurrentDomain.BaseDirectory;
            string engine = Path.Combine(dir, "OsuMapEngine.exe");
            if (File.Exists(engine)) return engine;

            DirectoryInfo parentInfo = Directory.GetParent(dir);
            string parentDir = parentInfo != null ? parentInfo.FullName : dir;
            engine = Path.Combine(parentDir, "OsuMapEngine.exe");
            if (File.Exists(engine)) return engine;

            string distDir = Path.Combine(dir, "dist");
            engine = Path.Combine(distDir, "OsuMapEngine.exe");
            if (File.Exists(engine)) return engine;

            // Self-contained standalone mode: extract embedded engine to %LOCALAPPDATA%\OsuMapHandler\
            try
            {
                string appData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
                string targetDir = Path.Combine(appData, "OsuMapHandler");
                string targetEngine = Path.Combine(targetDir, "OsuMapEngine.exe");

                var asm = Assembly.GetExecutingAssembly();
                using (var stream = asm.GetManifestResourceStream("OsuMapEngine.exe"))
                {
                    if (stream != null)
                    {
                        if (!Directory.Exists(targetDir))
                            Directory.CreateDirectory(targetDir);

                        if (File.Exists(targetEngine))
                        {
                            var fi = new FileInfo(targetEngine);
                            if (fi.Length == stream.Length)
                                return targetEngine;
                        }

                        using (var fs = new FileStream(targetEngine, FileMode.Create, FileAccess.Write, FileShare.None))
                        {
                            stream.CopyTo(fs);
                        }
                        return targetEngine;
                    }
                }
            }
            catch (Exception ex)
            {
                Log("Embedded engine extraction failed: " + ex.Message);
            }

            return null;
        }

        private static void RunEngine(string arguments, bool waitForExit = false)
        {
            string engine = GetEnginePath();
            if (string.IsNullOrEmpty(engine))
            {
                Log("Error: OsuMapEngine.exe not found!");
                return;
            }

            var psi = new ProcessStartInfo
            {
                FileName = engine,
                Arguments = arguments,
                UseShellExecute = false,
                CreateNoWindow = true
            };

            if (waitForExit)
            {
                psi.RedirectStandardOutput = true;
                psi.RedirectStandardError = true;
                try
                {
                    var p = Process.Start(psi);
                    if (p != null)
                    {
                        string stdout = p.StandardOutput.ReadToEnd();
                        string stderr = p.StandardError.ReadToEnd();
                        p.WaitForExit();
                        if (!string.IsNullOrEmpty(stdout))
                        {
                            Console.Out.Write(stdout);
                        }
                        if (!string.IsNullOrEmpty(stderr))
                        {
                            Console.Error.Write(stderr);
                        }
                    }
                }
                catch (Exception ex)
                {
                    Log("Failed to run engine: " + ex.Message);
                }
            }
            else
            {
                try
                {
                    Process.Start(psi);
                }
                catch (Exception ex)
                {
                    Log("Failed to start engine: " + ex.Message);
                }
            }
        }

        [STAThread]
        static void Main(string[] args)
        {
            // CRITICAL: Immediate input & foreground snapshot (< 5ms)
            // Evaluates physically held Shift key or key pressed during the click event!
            bool shiftHeld = IsShiftPhysicallyDown();
            string fgProc;
            string fgTitle;
            GetForegroundInfo(out fgProc, out fgTitle);

            if (args == null || args.Length == 0)
            {
                // Double-click -> Run setup GUI
                RunEngine("", waitForExit: false);
                return;
            }

            string first = (args[0] ?? "").Trim('\"', '\'', ' ');

            // CLI options -> attach to parent console and forward to engine
            if (first.StartsWith("-") || first.StartsWith("/"))
            {
                AttachConsole(ATTACH_PARENT_PROCESS);
                try
                {
                    var stdOut = Console.OpenStandardOutput();
                    var sw = new StreamWriter(stdOut) { AutoFlush = true };
                    Console.SetOut(sw);
                    var stdErr = Console.OpenStandardError();
                    var swErr = new StreamWriter(stdErr) { AutoFlush = true };
                    Console.SetError(swErr);
                }
                catch { }

                var sb = new StringBuilder();
                foreach (var a in args)
                {
                    if (sb.Length > 0) sb.Append(" ");
                    sb.Append("\"").Append(a).Append("\"");
                }
                RunEngine(sb.ToString(), waitForExit: true);
                return;
            }

            // URL handling & protocol normalization
            string url = first;
            if (url.StartsWith("osu-map://", StringComparison.OrdinalIgnoreCase))
                url = "https://" + url.Substring(10);
            else if (url.StartsWith("osu://b/", StringComparison.OrdinalIgnoreCase))
                url = "https://osu.ppy.sh/b/" + url.Substring(8);
            else if (url.StartsWith("osu://s/", StringComparison.OrdinalIgnoreCase))
                url = "https://osu.ppy.sh/s/" + url.Substring(8);
            else if (url.StartsWith("osu://dl/", StringComparison.OrdinalIgnoreCase))
                url = "https://osu.ppy.sh/d/" + url.Substring(9);
            else if (url.StartsWith("osu://d/", StringComparison.OrdinalIgnoreCase))
                url = "https://osu.ppy.sh/d/" + url.Substring(8);

            // Also check direct mirror URLs from chat (e.g. nerinyan, beatconnect, catboy)
            var mirrorMatch = Regex.Match(url, @"^https?://(?:nerinyan\.moe|api\.nerinyan\.moe|catboy\.best|beatconnect\.io)/(?:d|b)/(\d+)", RegexOptions.IgnoreCase);
            if (mirrorMatch.Success)
            {
                url = "https://osu.ppy.sh/beatmapsets/" + mirrorMatch.Groups[1].Value;
            }

            bool isBeatmap = BeatmapRegex.IsMatch(url);

            if (!isBeatmap)
            {
                Log("Non-beatmap link: forwarding to browser: " + url);
                OpenInBrowser(url);
                return;
            }

            if (shiftHeld)
            {
                Log("Shift held -> Forwarding beatmap link to browser: " + url);
                OpenInBrowser(url);
                return;
            }

            bool fromOsu = IsOsuForeground(fgProc, fgTitle);
            if (!fromOsu)
            {
                Log("Clicked outside osu! (foreground was " + (fgProc ?? "null") + ", title: " + (fgTitle ?? "null") + ") -> Forwarding: " + url);
                OpenInBrowser(url);
                return;
            }

            // Normal click inside osu! on a beatmap -> Download & import via engine!
            RunEngine("--from-osu \"" + url + "\"", waitForExit: false);
        }
    }
}
