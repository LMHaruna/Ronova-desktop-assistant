using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Text;
using System.Windows.Forms;

[assembly: AssemblyTitle("若娜瓦桌面助手")]
[assembly: AssemblyProduct("Ronova Desktop Assistant")]
[assembly: AssemblyDescription("启动若娜瓦桌面助手")]
[assembly: System.Runtime.Versioning.TargetFramework(".NETFramework,Version=v4.8")]

internal static class Launcher
{
    private static readonly string Root = AppDomain.CurrentDomain.BaseDirectory;
    private const string Title = "若娜瓦桌面助手";
    private const string Probe = "import sys,sysconfig; sys.exit(not (sys.implementation.name == 'cpython' and sys.version_info[:2] == (3,11) and sysconfig.get_platform() == 'win-amd64' and sys.prefix == sys.base_prefix))";

    [STAThread]
    private static int Main(string[] args)
    {
        try
        {
            string app = Path.Combine(Root, "client_app.py");
            string setup = Path.Combine(Root, "scripts", "setup_desktop.py");
            if (!File.Exists(setup) || !File.Exists(app))
                throw new IOException("请将 Ronova.exe 放在完整解压后的若娜瓦项目目录中。");

            string explicitPython = Environment.GetEnvironmentVariable("RONOVA_PYTHON");
            string python = String.IsNullOrWhiteSpace(explicitPython)
                ? Path.Combine(Root, @"runtime\python\cpython-3.11.17-windows-x86_64-none\python.exe")
                : explicitPython;
            string prefix = "";
            if (!CanUsePython(python, prefix))
            {
                if (!String.IsNullOrWhiteSpace(explicitPython))
                    throw new IOException("RONOVA_PYTHON 必须指向 Windows x64 CPython 3.11 基础解释器。");
                python = "py.exe";
                prefix = "-3.11 ";
                if (!CanUsePython(python, prefix))
                {
                    python = FindPythonOnPath();
                    prefix = "";
                    if (python == null)
                        throw new IOException("缺少内置 Python。请完整解压 Windows 运行包，或安装 Windows x64 CPython 3.11。");
                }
            }

            string detail;
            if (Run(python, prefix + "-EsB -X utf8 " + Quote(setup) + " --ensure", out detail) != 0)
                throw new IOException("环境准备失败。\n\n" + detail);

            StringBuilder arguments = new StringBuilder("-EsB -X utf8 ").Append(Quote(app));
            foreach (string arg in args)
                arguments.Append(' ').Append(Quote(arg));
            using (Process client = Process.Start(StartInfo(Path.Combine(Root,
                @"runtime\voice-venv\Scripts\pythonw.exe"), arguments.ToString())))
            {
                client.WaitForExit();
                return client.ExitCode;
            }
        }
        catch (Exception error)
        {
            MessageBox.Show(error.Message, Title, MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }

    private static bool CanUsePython(string executable, string prefix)
    {
        if (Path.IsPathRooted(executable) && !File.Exists(executable))
            return false;
        try
        {
            string detail;
            return Run(executable, prefix + "-IB -c " + Quote(Probe), out detail) == 0;
        }
        catch (System.ComponentModel.Win32Exception)
        {
            return false;
        }
    }

    private static string FindPythonOnPath()
    {
        foreach (string entry in (Environment.GetEnvironmentVariable("PATH") ?? "").Split(Path.PathSeparator))
        {
            string directory = entry.Trim().Trim('"');
            if (!Path.IsPathRooted(directory) || directory.IndexOf("Microsoft\\WindowsApps", StringComparison.OrdinalIgnoreCase) >= 0)
                continue;
            foreach (string name in new[] { "python3.11.exe", "python.exe" })
            {
                string candidate = Path.Combine(directory, name);
                if (CanUsePython(candidate, ""))
                    return candidate;
            }
        }
        return null;
    }

    private static ProcessStartInfo StartInfo(string executable, string arguments)
    {
        var info = new ProcessStartInfo(executable, arguments) {
            WorkingDirectory = Root, UseShellExecute = false, CreateNoWindow = true
        };
        info.EnvironmentVariables.Remove("PYTHONHOME");
        info.EnvironmentVariables.Remove("PYTHONPATH");
        info.EnvironmentVariables["PYTHONUTF8"] = "1";
        info.EnvironmentVariables["PYTHONDONTWRITEBYTECODE"] = "1";
        info.EnvironmentVariables["PYTHONNOUSERSITE"] = "1";
        info.EnvironmentVariables["PYTHON_MANAGER_AUTOMATIC_INSTALL"] = "false";
        return info;
    }

    private static int Run(string executable, string arguments, out string detail)
    {
        var info = StartInfo(executable, arguments);
        info.RedirectStandardOutput = true;
        info.RedirectStandardError = true;
        info.StandardOutputEncoding = Encoding.UTF8;
        info.StandardErrorEncoding = Encoding.UTF8;
        using (Process process = Process.Start(info))
        {
            var output = process.StandardOutput.ReadToEndAsync();
            var errors = process.StandardError.ReadToEndAsync();
            process.WaitForExit();
            string errorText = errors.Result.Trim();
            detail = errorText.Length == 0 ? output.Result.Trim() : errorText;
            if (detail.Length > 2400)
                detail = detail.Substring(detail.Length - 2400);
            return process.ExitCode;
        }
    }

    private static string Quote(string value)
    {
        // Windows doubles backslashes immediately before a quote.
        return "\"" + System.Text.RegularExpressions.Regex.Replace(value, "(\\\\*)(\"|$)",
            match => match.Groups[1].Value + match.Groups[1].Value
                + (match.Groups[2].Length == 0 ? "" : "\\\"")) + "\"";
    }
}
