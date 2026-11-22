; SPDX-License-Identifier: GPL-3.0-or-later

Unicode True
!include "MUI2.nsh"
!include "x64.nsh"

!define PRODUCT "RemoteQGIS Host Manager"
!define APP "${PRODUCT}.exe"
!define UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\RemoteQGIS Host Manager"

Name "${PRODUCT}"
OutFile "${OUTPUT}"
InstallDir "$LOCALAPPDATA\Programs\${PRODUCT}"
RequestExecutionLevel user
SetCompressor /SOLID lzma
AutoCloseWindow True

!define MUI_ICON "${ICON}"
!define MUI_UNICON "${ICON}"
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "SimpChinese"

Function .onInit
    ${IfNot} ${RunningX64}
        MessageBox MB_ICONSTOP "此安装程序需要 64 位 Windows。"
        Abort
    ${EndIf}
    ${If} ${FileExists} "$LOCALAPPDATA\RemoteQGIS Host\manager.lock"
    ${OrIf} ${FileExists} "$LOCALAPPDATA\RemoteQGIS Host\window.lock"
        MessageBox MB_OKCANCEL|MB_ICONINFORMATION "如果旧版正在运行，请点击「退出管理服务」，再从托盘退出管理界面。没有托盘菜单的旧版可直接关闭窗口。完成后点击确定。" IDOK continue
        Abort
    ${EndIf}
    continue:
FunctionEnd

Section
    SetShellVarContext current
    SetOutPath "$INSTDIR"
    File "${PAYLOAD}/${APP}"
    File /r "${PAYLOAD}/app"
    File "${PAYLOAD}/使用说明.md"
    WriteUninstaller "$INSTDIR\Uninstall.exe"
    CreateShortcut "$SMPROGRAMS\${PRODUCT}.lnk" "$INSTDIR\${APP}"
    WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayName" "${PRODUCT}"
    WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayVersion" "${VERSION}"
    WriteRegStr HKCU "${UNINSTALL_KEY}" "InstallLocation" "$INSTDIR"
    WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayIcon" "$INSTDIR\${APP}"
    WriteRegStr HKCU "${UNINSTALL_KEY}" "UninstallString" '$\"$INSTDIR\Uninstall.exe$\"'
    IfSilent done
    ExecShell "open" "$INSTDIR\${APP}" "--choose-qgis"
    done:
SectionEnd

Section "Uninstall"
    SetShellVarContext current
    ${If} ${FileExists} "$LOCALAPPDATA\RemoteQGIS Host\manager.lock"
    ${OrIf} ${FileExists} "$LOCALAPPDATA\RemoteQGIS Host\window.lock"
        MessageBox MB_OKCANCEL|MB_ICONINFORMATION "请在 Host Manager 中关闭登录自启，退出管理服务，再从托盘退出管理界面，然后点击确定。" IDOK uninstall
        Abort
    ${EndIf}
    uninstall:
    ClearErrors
    Delete "$INSTDIR\${APP}"
    IfErrors stop
    Delete "$SMPROGRAMS\${PRODUCT}.lnk"
    RMDir /r "$INSTDIR\app"
    Delete "$INSTDIR\使用说明.md"
    DeleteRegKey HKCU "${UNINSTALL_KEY}"
    Delete "$INSTDIR\Uninstall.exe"
    RMDir "$INSTDIR"
    Goto done
    stop:
    MessageBox MB_ICONSTOP "请先退出管理服务并关闭窗口，然后重新卸载。"
    Abort
    done:
SectionEnd
