// SPDX-License-Identifier: GPL-3.0-or-later

//go:build windows

package main

import (
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"unsafe"
)

func showError(message string) {
	user := syscall.NewLazyDLL("user32.dll")
	box := user.NewProc("MessageBoxW")
	title, _ := syscall.UTF16PtrFromString("RemoteQGIS Host Manager")
	body, _ := syscall.UTF16PtrFromString(message)
	box.Call(0, uintptr(unsafe.Pointer(body)), uintptr(unsafe.Pointer(title)), 0x10)
}

func existing(path string) bool {
	info, err := os.Stat(path)
	return err == nil && !info.IsDir()
}

func installations() []string {
	if path := os.Getenv("REMOTEQGIS_QGIS_ROOT"); path != "" {
		return []string{path}
	}
	var roots []string
	for _, base := range []string{os.Getenv("ProgramW6432"), os.Getenv("ProgramFiles"), os.Getenv("LOCALAPPDATA")} {
		if base == "" {
			continue
		}
		list, _ := filepath.Glob(filepath.Join(base, "QGIS 3.*"))
		sort.Sort(sort.Reverse(sort.StringSlice(list)))
		for _, root := range list {
			version := strings.Split(strings.TrimPrefix(filepath.Base(root), "QGIS 3."), ".")
			minor, err := strconv.Atoi(version[0])
			if err == nil && minor >= 40 {
				roots = append(roots, root)
			}
		}
		roots = append(roots, filepath.Join(base, "OSGeo4W"))
	}
	return append(roots, filepath.Join(os.Getenv("SystemDrive")+string(os.PathSeparator), "OSGeo4W"))
}

func locate() (string, string) {
	roots := installations()
	if data, err := os.ReadFile(choiceFile()); err == nil && os.Getenv("REMOTEQGIS_QGIS_ROOT") == "" {
		roots = append([]string{strings.TrimSpace(string(data))}, roots...)
	}
	for _, root := range roots {
		for _, flavor := range []string{"qgis-ltr", "qgis"} {
			batch := filepath.Join(root, "bin", "python-"+flavor+".bat")
			desktop := filepath.Join(root, "bin", flavor+"-bin.exe")
			if existing(batch) && existing(desktop) {
				return batch, desktop
			}
		}
	}
	return "", ""
}

func choiceFile() string {
	return filepath.Join(os.Getenv("LOCALAPPDATA"), "RemoteQGIS Host", "qgis-installation.txt")
}

func chooseQGIS() (string, string) {
	command := `Add-Type -AssemblyName System.Windows.Forms
[Console]::OutputEncoding = [Text.UTF8Encoding]::new()
$dialog = New-Object System.Windows.Forms.OpenFileDialog
$dialog.Title = 'Select the installed QGIS Python launcher (in the QGIS bin folder)'
$dialog.Filter = 'QGIS Python launcher|python-qgis-ltr.bat;python-qgis.bat'
if ($dialog.ShowDialog() -eq 'OK') { [Console]::WriteLine($dialog.FileName) }`
	shell := exec.Command(filepath.Join(os.Getenv("SystemRoot"), "System32", "WindowsPowerShell", "v1.0", "powershell.exe"),
		"-NoProfile", "-STA", "-Command", command)
	shell.SysProcAttr = &syscall.SysProcAttr{CreationFlags: 0x08000000}
	result, err := shell.Output()
	if err != nil {
		return "", ""
	}
	batch := strings.TrimSpace(string(result))
	flavor := strings.TrimSuffix(strings.TrimPrefix(strings.ToLower(filepath.Base(batch)), "python-"), ".bat")
	if flavor != "qgis-ltr" && flavor != "qgis" {
		return "", ""
	}
	desktop := filepath.Join(filepath.Dir(batch), flavor+"-bin.exe")
	if !existing(batch) || !existing(desktop) {
		return "", ""
	}
	if os.Getenv("LOCALAPPDATA") != "" {
		if os.MkdirAll(filepath.Dir(choiceFile()), 0700) == nil {
			_ = os.WriteFile(choiceFile(), []byte(filepath.Dir(filepath.Dir(batch))), 0600)
		}
	}
	return batch, desktop
}

func daemon() bool {
	for _, arg := range os.Args[1:] {
		if strings.EqualFold(arg, "--daemon") {
			return true
		}
	}
	return false
}

func run() error {
	self, err := os.Executable()
	if err != nil {
		return err
	}
	batch, desktop := locate()
	if batch == "" && !daemon() {
		batch, desktop = chooseQGIS()
	}
	if batch == "" {
		return fmt.Errorf("需要 QGIS 3.40 或更高的 3.x 版本。请先安装 QGIS LTR。自定义安装位置可以设置 REMOTEQGIS_QGIS_ROOT")
	}
	source := filepath.Join(filepath.Dir(self), "app")
	bootstrap := filepath.Join(source, "host_manager", "windows_bootstrap.py")
	if !existing(bootstrap) {
		return fmt.Errorf("请解压完整文件夹，并从同一目录打开 RemoteQGIS Host Manager.exe")
	}
	args, err := json.Marshal(os.Args[1:])
	if err != nil {
		return err
	}
	flags := uint32(0x08000000) // CREATE_NO_WINDOW
	cmd := exec.Command(filepath.Join(os.Getenv("SystemRoot"), "System32", "cmd.exe"))
	cmd.SysProcAttr = &syscall.SysProcAttr{
		CreationFlags: flags,
		CmdLine:       `cmd.exe /d /v:off /s /c ""%REMOTEQGIS_PYQGIS%" "%REMOTEQGIS_BOOTSTRAP%""`,
	}
	cmd.Dir = source
	cmd.Env = append(os.Environ(), "REMOTEQGIS_MANAGER_LAUNCHER="+self, "REMOTEQGIS_DESKTOP="+desktop,
		"REMOTEQGIS_PYQGIS="+batch, "REMOTEQGIS_BOOTSTRAP="+bootstrap,
		"REMOTEQGIS_WINDOWS_ARGS="+string(args), "PYTHONNOUSERSITE=1", "PYTHONDONTWRITEBYTECODE=1", "PYTHONUTF8=1")
	cmd.Stdin, cmd.Stdout, cmd.Stderr = os.Stdin, os.Stdout, os.Stderr
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("无法启动 QGIS Python 运行环境（%w）", err)
	}
	return nil
}

func main() {
	if err := run(); err != nil {
		if daemon() {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		showError(err.Error())
		os.Exit(1)
	}
}
