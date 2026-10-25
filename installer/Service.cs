// RTWorkshopService.exe - Windows service that runs "tools\python\python.exe server.py" with no window,
// writes its output to data\logs\server.log and restarts it if it stops unexpectedly.
// Built by build.ps1 with the C# compiler that ships with Windows (C# 5).
using System;
using System.Diagnostics;
using System.IO;
using System.ServiceProcess;
using System.Text;
using System.Threading;

public class RTWorkshopService : ServiceBase
{
    readonly string root = AppDomain.CurrentDomain.BaseDirectory;
    readonly object gate = new object();
    Process proc;
    Timer restart;
    volatile bool stopping;

    public RTWorkshopService()
    {
        ServiceName = "RTWorkshop";
        CanStop = true;
        CanShutdown = true;
    }

    string LogPath { get { return Path.Combine(root, "data", "logs", "server.log"); } }

    void Log(string line)
    {
        if (line == null) return;
        lock (gate)
        {
            try
            {
                var fi = new FileInfo(LogPath);
                if (fi.Exists && fi.Length > 5 * 1024 * 1024)
                {
                    File.Copy(LogPath, LogPath + ".old", true);
                    File.Delete(LogPath);
                }
                File.AppendAllText(LogPath, DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss ") + line + Environment.NewLine, Encoding.UTF8);
            }
            catch { }
        }
    }

    void Launch()
    {
        lock (gate)
        {
            if (stopping) return;
            var psi = new ProcessStartInfo(Path.Combine(root, "tools", "python", "python.exe"), "-u server.py");
            psi.WorkingDirectory = root;
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            psi.RedirectStandardOutput = true;
            psi.RedirectStandardError = true;
            psi.StandardOutputEncoding = Encoding.UTF8;
            psi.StandardErrorEncoding = Encoding.UTF8;
            psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
            proc = new Process();
            proc.StartInfo = psi;
            proc.EnableRaisingEvents = true;
            proc.OutputDataReceived += (s, e) => Log(e.Data);
            proc.ErrorDataReceived += (s, e) => Log(e.Data);
            proc.Exited += (s, e) =>
            {
                Log("server stopped (exit code " + ((Process)s).ExitCode + ")");
                if (!stopping) restart = new Timer(_ => Launch(), null, 5000, Timeout.Infinite);
            };
            try
            {
                proc.Start();
                proc.BeginOutputReadLine();
                proc.BeginErrorReadLine();
                Log("server started (pid " + proc.Id + ")");
            }
            catch (Exception ex)
            {
                Log("could not start server: " + ex.Message);
                restart = new Timer(_ => Launch(), null, 10000, Timeout.Infinite);
            }
        }
    }

    protected override void OnStart(string[] args)
    {
        Directory.CreateDirectory(Path.Combine(root, "data", "logs"));
        stopping = false;
        Launch();
    }

    protected override void OnStop()
    {
        stopping = true;
        try
        {
            if (proc != null && !proc.HasExited)
            {
                proc.Kill();
                proc.WaitForExit(5000);
            }
        }
        catch { }
        Log("service stopped");
    }

    protected override void OnShutdown() { OnStop(); }

    public static void Main() { ServiceBase.Run(new RTWorkshopService()); }
}
