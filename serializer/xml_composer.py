import datetime
import json
import os
import re
import xml.etree.ElementTree as ET


class WorkflowXmlComposer:
    """
    NAMESPACE_REGISTRY format:
      None  — built-in WF activity or confirmed-working-without-xmlns activity.
              Tag is serialized WITHOUT a namespace prefix.
      str   — full CLR namespace string from namespace_registry.json.
              Tag gets a generated xmlns:ns_<name> prefix and declaration.

    Activities confirmed working without xmlns prefix (imported successfully
    in sandbox testing) are kept as None even though their CLR strings are now
    known — changing them to prefixed tags risks breaking working imports.
    """

    NAMESPACE_REGISTRY = {
        # ── WF built-in control flow — no xmlns needed ────────────────────
        "IfElseActivity":       None,
        "IfElseBranchActivity": None,
        "SequenceActivity":     None,
        "ParallelActivity":     None,
        "WhileActivity":        None,
        "ForEachActivity":      None,
        "UserGroup":            None,

        # ── Confirmed working without prefix in sandbox ───────────────────
        "ReturnValue":      None,
        "Continue":         None,
        "SendEmail":        None,
        "ExitWhile":        None,
        "DisplayValue":     None,
        "GetRowsCount":     None,
        "GetCellValue":     None,
        "RunWorkflow":      None,
        "Ping":             None,
        "MemorySet":        None,
        "MultiMemorySet":   None,
        "DisplayMultiValue": None,
        "GetRows":          None,

        # ── All confirmed CLR namespaces from namespace_registry.json ─────
        "ADAddToGroup":           "clr-namespace:ADAddToGroup;Assembly=ADAddToGroup, Version=1.1.0.0, Culture=neutral, PublicKeyToken=null",
        "ADComputerLoggedInDate": "clr-namespace:ADComputerLoggedInDate;Assembly=ADComputerLoggedInDate, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "ADCreateAccount":        "clr-namespace:ADCreateAccount;Assembly=ADCreateAccount, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "ADDeleteAccount":        "clr-namespace:ADDeleteAccount;Assembly=ADDeleteAccount, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "ADDisableAccount":       "clr-namespace:ADDisableAccount;Assembly=ADDisableAccount, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "ADEnableAccount":        "clr-namespace:ADEnableAccount;Assembly=ADEnableAccount, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "ADGetProperty":          "clr-namespace:ADGetProperty;Assembly=ADGetProperty, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "ADGroupExists":          "clr-namespace:ADGroupExists;Assembly=ADGroupExists, Version=1.1.0.0, Culture=neutral, PublicKeyToken=null",
        "ADIsAccountDisabled":    "clr-namespace:ADIsAccountDisabled;Assembly=ADIsAccountDisabled, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "ADIsAccountLocked":      "clr-namespace:ADIsAccountLocked;Assembly=ADIsAccountLocked, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "ADListOU":               "clr-namespace:ADListOU;Assembly=ADListOU, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "ADPassExpDaysLeft":      "clr-namespace:ADPassExpDaysLeft;Assembly=ADPassExpDaysLeft, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "ADPasswordReset":        "clr-namespace:ADPasswordReset;Assembly=ADPasswordReset, Version=1.1.0.0, Culture=neutral, PublicKeyToken=null",
        "ADSearchUserLogonName":  "clr-namespace:ADSearchUserLogonName;Assembly=ADSearchUserLogonName, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "ADSetPasswordProperties":"clr-namespace:ADSetPasswordProperties;Assembly=ADSetPasswordProperties, Version=1.1.0.0, Culture=neutral, PublicKeyToken=null",
        "ADSetProperty":          "clr-namespace:ADSetProperty;Assembly=ADSetProperty, Version=1.1.0.0, Culture=neutral, PublicKeyToken=null",
        "ADUnlockAccount":        "clr-namespace:ADUnlockAccount;Assembly=ADUnlockAccount, Version=1.0.0.0, Culture=neutral, PublicKeyToken=null",
        "ADUserExists":           "clr-namespace:ADUserExists;Assembly=ADUserExists, Version=1.1.0.0, Culture=neutral, PublicKeyToken=null",
        "ADUserLoggedInDate":     "clr-namespace:ADUserLoggedInDate;Assembly=ADUserLoggedInDate, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "ADUsersSynchronization": "clr-namespace:ADUsersSynchronization;Assembly=ADUsersSynchronization, Version=4.7.0.0, Culture=neutral, PublicKeyToken=null",
        "AddMemoryTableRow":      "clr-namespace:AddMemoryTableRow;Assembly=AddMemoryTableRow, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "AdvancedCommunicate":    "clr-namespace:AdvancedCommunicate;Assembly=AdvancedCommunicate, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "ApplicationPoolList":    "clr-namespace:ApplicationPoolList;Assembly=ApplicationPoolList, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "ApplicationPoolRecycle": "clr-namespace:ApplicationPoolRecycle;Assembly=ApplicationPoolRecycle, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "ApplicationPoolStart":   "clr-namespace:ApplicationPoolStart;Assembly=ApplicationPoolStart, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "ApplicationPoolStatus":  "clr-namespace:ApplicationPoolStatus;Assembly=ApplicationPoolStatus, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "CPU":                    "clr-namespace:CPU;Assembly=CPU, Version=1.4.0.0, Culture=neutral, PublicKeyToken=null",
        "ChangeSeverity":         "clr-namespace:ChangeSeverity;Assembly=ChangeSeverity, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "CloseIncident":          "clr-namespace:CloseIncident;Assembly=CloseIncident, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "Communicate":            "clr-namespace:ActivityLibrary;Assembly=ActivityLibrary,     Version=3.6.0.0, Culture=neutral, PublicKeyToken=null",
        "ConditionalWait":        "clr-namespace:ConditionalWait;Assembly=ConditionalWait, Version=1.0.0.0, Culture=neutral, PublicKeyToken=null",
        "Contains":               "clr-namespace:Contains;Assembly=Contains, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "ConvertToHTMLTable":     "clr-namespace:ConvertToHTMLTable;Assembly=ConvertToHTMLTable, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "ConvertToPlainText":     "clr-namespace:ConvertToPlainText;Assembly=ConvertToPlainText, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "Counter":                "clr-namespace:Counter;Assembly=Counter, Version=4.6.0.0, Culture=neutral, PublicKeyToken=null",
        "CreateMemoryTable":      "clr-namespace:CreateMemoryTable;Assembly=CreateMemoryTable, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "DateDifference":         "clr-namespace:DateDifference;Assembly=DateDifference, Version=1.0.0.0, Culture=neutral, PublicKeyToken=null",
        "DeleteFile":             "clr-namespace:DeleteFile;Assembly=DeleteFile, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "DeleteFolder":           "clr-namespace:DeleteFolder;Assembly=DeleteFolder, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "DeleteMemoryTableColumns":"clr-namespace:DeleteMemoryTableColumns;Assembly=DeleteMemoryTableColumns, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "DeleteMemoryTableRows":  "clr-namespace:DeleteMemoryTableRows;Assembly=DeleteMemoryTableRows, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "DiskSpace":              "clr-namespace:DiskSpace;Assembly=DiskSpace, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "ESMGetEvent":            "clr-namespace:ESMGetEvent;Assembly=ESMGetEvent, Version=4.8.0.0, Culture=neutral, PublicKeyToken=null",
        "EnablePrivilegedCommands":"clr-namespace:EnablePrivilegedCommands;Assembly=EnablePrivilegedCommands, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "ExtractLineFromText":    "clr-namespace:ExtractLineFromText;Assembly=ExtractLineFromText, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "FTPDeleteFile":          "clr-namespace:FTPDeleteFile;Assembly=FTPDeleteFile, Version=1.0.0.0, Culture=neutral, PublicKeyToken=null",
        "FTPGetFile":             "clr-namespace:FTPGetFile;Assembly=FTPGetFile, Version=1.1.0.0, Culture=neutral, PublicKeyToken=null",
        "FTPListFolder":          "clr-namespace:FTPListFolder;Assembly=FTPListFolder, Version=1.0.0.0, Culture=neutral, PublicKeyToken=null",
        "FTPRenameFile":          "clr-namespace:FTPRenameFile;Assembly=FTPRenameFile, Version=1.0.0.0, Culture=neutral, PublicKeyToken=null",
        "FileAccessedDate":       "clr-namespace:FileAccessedDate;Assembly=FileAccessedDate, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "FileCheckSumComparison": "clr-namespace:FileCheckSumComparison;Assembly=FileCheckSumComparison, Version=1.5.0.0, Culture=neutral, PublicKeyToken=null",
        "FileCopy":               "clr-namespace:FileCopy;Assembly=FileCopy, Version=1.1.0.0, Culture=neutral, PublicKeyToken=null",
        "FileDownload":           "clr-namespace:FileDownload;Assembly=FileDownload, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "FileExist":              "clr-namespace:FileExist;Assembly=FileExist, Version=4.6.0.0, Culture=neutral, PublicKeyToken=null",
        "FileModifiedDate":       "clr-namespace:FileModifiedDate;Assembly=FileModifiedDate, Version=1.4.0.0, Culture=neutral, PublicKeyToken=null",
        "FileSize":               "clr-namespace:FileSize;Assembly=FileSize, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "FileVersion":            "clr-namespace:FileVersion;Assembly=FileVersion, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "FolderCopy":             "clr-namespace:FolderCopy;Assembly=FolderCopy, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "FolderList":             "clr-namespace:FolderList;Assembly=FolderList, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "FolderSize":             "clr-namespace:FolderSize;Assembly=FolderSize, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "FunctionCalculator":     "clr-namespace:FunctionCalculator;Assembly=FunctionCalculator, Version=4.6.0.0, Culture=neutral, PublicKeyToken=null",
        "GetColumnsCount":        "clr-namespace:GetColumnsCount;Assembly=GetColumnsCount, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "GetDate":                "clr-namespace:GetDate;Assembly=GetDate, Version=1.0.0.0, Culture=neutral, PublicKeyToken=null",
        "GetIncidentSeverity":    "clr-namespace:GetIncidentSeverity;Assembly=GetIncidentSeverity, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "GetInstalledSoftware":   "clr-namespace:GetInstalledSoftware;Assembly=GetInstalledSoftware, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "GetInterfacesStatus":    "clr-namespace:GetInterfacesStatus;Assembly=GetInterfacesStatus, Version=1.0.0.0, Culture=neutral, PublicKeyToken=null",
        "GetOpenIncidents":       "clr-namespace:GetOpenIncidents;Assembly=GetOpenIncidents, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "GetOperatingSystem":     "clr-namespace:GetOperatingSystem;Assembly=GetOperatingSystem, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "GetWindowEventLogs":     "clr-namespace:GetWindowEventLogs;Assembly=GetWindowEventLogs, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "GoTo":                   "clr-namespace:GoTo;Assembly=GoTo, Version=4.6.0.0, Culture=neutral, PublicKeyToken=null",
        "HyperVInfo":             "clr-namespace:HyperVInfo;Assembly=HyperVInfo, Version=4.8.0.0, Culture=neutral, PublicKeyToken=null",
        "HyperVPowerOFF":         "clr-namespace:HyperVPowerOFF;Assembly=HyperVPowerOFF, Version=4.8.0.0, Culture=neutral, PublicKeyToken=null",
        "HyperVPowerON":          "clr-namespace:HyperVPowerON;Assembly=HyperVPowerON, Version=4.8.0.0, Culture=neutral, PublicKeyToken=null",
        "HyperVPowerShell":       "clr-namespace:HyperVPowerShell;Assembly=HyperVPowerShell, Version=4.8.0.0, Culture=neutral, PublicKeyToken=null",
        "HyperVRestart":          "clr-namespace:HyperVRestart;Assembly=HyperVRestart, Version=4.8.0.0, Culture=neutral, PublicKeyToken=null",
        "HyperVShutDown":         "clr-namespace:HyperVShutDown;Assembly=HyperVShutDown, Version=4.8.0.0, Culture=neutral, PublicKeyToken=null",
        "IISReset":               "clr-namespace:IISReset;Assembly=IISReset, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "IncidentStatus":         "clr-namespace:IncidentStatus;Assembly=IncidentStatus, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "IsEmpty":                "clr-namespace:IsEmpty;Assembly=IsEmpty, Version=4.6.0.0, Culture=neutral, PublicKeyToken=null",
        "JiraGenericCommand":     "clr-namespace:JiraGenericCommand;Assembly=JiraGenericCommand, Version=4.8.0.0, Culture=neutral, PublicKeyToken=null",
        "JiraGetIssue":           "clr-namespace:JiraGetIssue;Assembly=JiraGetIssue, Version=4.7.0.0, Culture=neutral, PublicKeyToken=null",
        "JiraUpdateIssue":        "clr-namespace:JiraUpdateIssue;Assembly=JiraUpdateIssue, Version=4.7.0.0, Culture=neutral, PublicKeyToken=null",
        "LastResponse":           "clr-namespace:LastResponse;Assembly=LastResponse, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "Left":                   "clr-namespace:Left;Assembly=Left, Version=4.6.0.0, Culture=neutral, PublicKeyToken=null",
        "Memory":                 "clr-namespace:Memory;Assembly=Memory, Version=1.4.0.0, Culture=neutral, PublicKeyToken=null",
        "MemoryTableComparison":  "clr-namespace:MemoryTableComparison;Assembly=MemoryTableComparison, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "NetAppBreakSnapmirror":  "clr-namespace:NetAppBreakSnapmirror;Assembly=NetAppBreakSnapmirror, Version=4.7.0.0, Culture=neutral, PublicKeyToken=null",
        "NetAppCloneVolume":      "clr-namespace:NetAppCloneVolume;Assembly=NetAppCloneVolume, Version=4.7.0.0, Culture=neutral, PublicKeyToken=null",
        "NetAppCreateBasicVolume":"clr-namespace:NetAppCreateBasicVolume;Assembly=NetAppCreateBasicVolume, Version=4.7.0.0, Culture=neutral, PublicKeyToken=null",
        "NetAppCreateExportPolicy":"clr-namespace:NetAppCreateExportPolicy;Assembly=NetAppCreateExportPolicy, Version=4.7.0.0, Culture=neutral, PublicKeyToken=null",
        "NetAppCreateExportRule": "clr-namespace:NetAppCreateExportRule;Assembly=NetAppCreateExportRule, Version=4.7.0.0, Culture=neutral, PublicKeyToken=null",
        "NetAppCreateLun":        "clr-namespace:NetAppCreateLun;Assembly=NetAppCreateLun, Version=4.7.0.0, Culture=neutral, PublicKeyToken=null",
        "NetAppCreateSnapmirror": "clr-namespace:NetAppCreateSnapmirror;Assembly=NetAppCreateSnapmirror, Version=4.7.0.0, Culture=neutral, PublicKeyToken=null",
        "NetAppGetObject":        "clr-namespace:NetAppGetObject;Assembly=NetAppGetObject, Version=4.8.0.0, Culture=neutral, PublicKeyToken=null",
        "NetAppInitializeSnapmirror":"clr-namespace:NetAppInitializeSnapmirror;Assembly=NetAppInitializeSnapmirror, Version=4.7.0.0, Culture=neutral, PublicKeyToken=null",
        "NetAppMapLun":           "clr-namespace:NetAppMapLun;Assembly=NetAppMapLun, Version=4.7.0.0, Culture=neutral, PublicKeyToken=null",
        "NetAppResizeVolume":     "clr-namespace:NetAppResizeVolume;Assembly=NetAppResizeVolume, Version=4.7.0.0, Culture=neutral, PublicKeyToken=null",
        "PasswordGenerator":      "clr-namespace:PasswordGenerator;Assembly=PasswordGenerator, Version=1.0.0.0, Culture=neutral, PublicKeyToken=null",
        "PlayAudio":              "clr-namespace:PlayAudio;Assembly=PlayAudio, Version=1.1.0.0, Culture=neutral, PublicKeyToken=null",
        "PowerShell":             "clr-namespace:PowerShell;Assembly=PowerShell, Version=4.7.0.0, Culture=neutral, PublicKeyToken=null",
        "PowerShellScript":       "clr-namespace:PowerShellScript;Assembly=PowerShellScript, Version=4.6.1.0, Culture=neutral, PublicKeyToken=null",
        "ProcessCounter":         "clr-namespace:ProcessCounter;Assembly=ProcessCounter, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "ProcessKill":            "clr-namespace:ProcessKill;Assembly=ProcessKill, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "ProcessList":            "clr-namespace:ProcessList;Assembly=ProcessList, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "ReadContinuousFile":     "clr-namespace:ReadContinuousFile;Assembly=ReadContinuousFile, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "ReadFile":               "clr-namespace:ReadFile;Assembly=ReadFile, Version=1.4.0.0, Culture=neutral, PublicKeyToken=null",
        "ReadXLS":                "clr-namespace:ReadXLS;Assembly=ReadXLS, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "ReplaceString":          "clr-namespace:ReplaceString;Assembly=ReplaceString, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "ReplaceStringAdvanced":  "clr-namespace:ReplaceStringAdvanced;Assembly=ReplaceStringAdvanced, Version=4.6.1.0, Culture=neutral, PublicKeyToken=null",
        "ResultSetFilter":        "clr-namespace:ResultSetFilter;Assembly=ResultSetFilter, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "SNCreateRecord":         "clr-namespace:SNCreateRecord;Assembly=SNCreateRecord, Version=4.6.1.0, Culture=neutral, PublicKeyToken=null",
        "SelfServiceResponse":    "clr-namespace:SelfServiceResponse;Assembly=SelfServiceResponse, Version=4.7.0.0, Culture=neutral, PublicKeyToken=null",
        "SendCiscoCommand":       "clr-namespace:SendCiscoCommand;Assembly=SendCiscoCommand, Version=1.0.0.0, Culture=neutral, PublicKeyToken=null",
        "SendEyeShareIM":         "clr-namespace:SendEyeShareIM;Assembly=SendEyeShareIM, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "SendSMS":                "clr-namespace:SendSMS;Assembly=SendSMS, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "SendSSHCommand":         "clr-namespace:SendSSHCommand;Assembly=SendSSHCommand, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "ServerRestart":          "clr-namespace:ServerRestart;Assembly=ServerRestart, Version=4.6.0.0, Culture=neutral, PublicKeyToken=null",
        "ServerShutdown":         "clr-namespace:ServerShutdown;Assembly=ServerShutdown, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "ServerStandby":          "clr-namespace:ServerStandby;Assembly=ServerStandby, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "ServiceList":            "clr-namespace:ServiceList;Assembly=ServiceList, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "ServiceStart":           "clr-namespace:ServiceStart;Assembly=ServiceStart, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "ServiceStatus":          "clr-namespace:ServiceStatus;Assembly=ServiceStatus, Version=1.4.0.0, Culture=neutral, PublicKeyToken=null",
        "ServiceStop":            "clr-namespace:ServiceStop;Assembly=ServiceStop, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "SetCellValue":           "clr-namespace:SetCellValue;Assembly=SetCellValue, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "SetServiceLogonCredentials":"clr-namespace:SetServiceLogonCredentials;Assembly=SetServiceLogonCredentials, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "SetServiceStartUpType":  "clr-namespace:SetServiceStartUpType;Assembly=SetServiceStartUpType, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "SortTable":              "clr-namespace:SortTable;Assembly=SortTable, Version=1.0.0.0, Culture=neutral, PublicKeyToken=null",
        "Split":                  "clr-namespace:Split;Assembly=Split, Version=1.1.0.0, Culture=neutral, PublicKeyToken=null",
        "StartCiscoSession":      "clr-namespace:StartCiscoSession;Assembly=StartCiscoSession, Version=1.1.0.0, Culture=neutral, PublicKeyToken=null",
        "StartFTPSession":        "clr-namespace:StartFTPSession;Assembly=StartFTPSession, Version=1.0.0.0, Culture=neutral, PublicKeyToken=null",
        "StartIVRSession":        "clr-namespace:StartIVRSession;Assembly=StartIVRSession, Version=1.1.0.0, Culture=neutral, PublicKeyToken=null",
        "StartSSHSession":        "clr-namespace:StartSSHSession;Assembly=StartSSHSession, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "SubString":              "clr-namespace:SubString;Assembly=SubString, Version=4.6.0.0, Culture=neutral, PublicKeyToken=null",
        "SubStringByText":        "clr-namespace:SubStringByText;Assembly=SubStringByText, Version=4.6.0.0, Culture=neutral, PublicKeyToken=null",
        "SystemUptime":           "clr-namespace:SystemUptime;Assembly=SystemUptime, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "TSQLQuery":              "clr-namespace:TSQLQuery;Assembly=TSQLQuery, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "TSQLStatement":          "clr-namespace:TSQLStatement;Assembly=TSQLStatement, Version=4.7.0.1, Culture=neutral, PublicKeyToken=null",
        "Terminate":              "clr-namespace:Terminate;Assembly=Terminate, Version=1.0.0.0, Culture=neutral, PublicKeyToken=null",
        "TerminateCiscoSession":  "clr-namespace:TerminateCiscoSession;Assembly=TerminateCiscoSession, Version=1.0.0.0, Culture=neutral, PublicKeyToken=null",
        "TerminateFTPSession":    "clr-namespace:TerminateFTPSession;Assembly=TerminateFTPSession, Version=1.0.0.0, Culture=neutral, PublicKeyToken=null",
        "TerminateSSHSession":    "clr-namespace:TerminateSSHSession;Assembly=TerminateSSHSession, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "TerminateWorkflow":      "clr-namespace:TerminateWorkflow;Assembly=TerminateWorkflow, Version=4.6.0.0, Culture=neutral, PublicKeyToken=null",
        "Trim":                   "clr-namespace:Trim;Assembly=Trim, Version=4.6.0.0, Culture=neutral, PublicKeyToken=null",
        "URLCheck":               "clr-namespace:URLCheck;Assembly=URLCheck, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "VMClone":                "clr-namespace:VMClone;Assembly=VMClone, Version=1.0.0.0, Culture=neutral, PublicKeyToken=null",
        "VMCreateSnapshot":       "clr-namespace:VMCreateSnapshot;Assembly=VMCreateSnapshot, Version=1.0.0.0, Culture=neutral, PublicKeyToken=null",
        "VMDeleteSnapshot":       "clr-namespace:VMDeleteSnapshot;Assembly=VMDeleteSnapshot, Version=1.0.0.0, Culture=neutral, PublicKeyToken=null",
        "VMHostList":             "clr-namespace:VMHostList;Assembly=VMHostList, Version=1.0.0.0, Culture=neutral, PublicKeyToken=null",
        "VMInfo":                 "clr-namespace:VMInfo;Assembly=VMInfo, Version=1.0.0.0, Culture=neutral, PublicKeyToken=null",
        "VMList":                 "clr-namespace:VMList;Assembly=VMList, Version=1.0.0.0, Culture=neutral, PublicKeyToken=null",
        "VMListSnapshot":         "clr-namespace:VMListSnapshot;Assembly=VMListSnapshot, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "VMPowerState":           "clr-namespace:VMPowerState;Assembly=VMPowerState, Version=1.0.0.0, Culture=neutral, PublicKeyToken=null",
        "VMSnapshotInfo":         "clr-namespace:VMSnapshotInfo;Assembly=VMSnapshotInfo, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "WMI":                    "clr-namespace:WMI;Assembly=WMI, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "Wait":                   "clr-namespace:Wait;Assembly=Wait, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "WaitForDTMF":            "clr-namespace:WaitForDTMF;Assembly=WaitForDTMF, Version=1.0.0.0, Culture=neutral, PublicKeyToken=null",
        "WakeOnLan":              "clr-namespace:WakeOnLan;Assembly=WakeOnLan, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "WriteFile":              "clr-namespace:WriteFile;Assembly=WriteFile, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "WriteXLS":               "clr-namespace:WriteXLS;Assembly=WriteXLS, Version=1.1.0.0, Culture=neutral, PublicKeyToken=null",
        "ZipCompress":            "clr-namespace:ZipCompress;Assembly=ZipCompress, Version=2.0.0.0, Culture=neutral, PublicKeyToken=null",
        "ZipDecompress":          "clr-namespace:ZipDecompress;Assembly=ZipDecompress, Version=1.1.0.0, Culture=neutral, PublicKeyToken=null",
    }

    SKIP_FIELDS = {
        "CustomTypeName",
        "modulePermissions",
        "notes",
    }

    # Control flow containers — do NOT get standard activity defaults
    NO_DEFAULTS = {
        "IfElseActivity", "IfElseBranchActivity", "SequenceActivity",
        "ParallelActivity", "WhileActivity", "ForEachActivity",
        "UserGroup", "ExitWhile", "ReturnValue",
    }

    SEQUENCE_CONTAINERS = {"WhileActivity", "ForEachActivity"}

    # Standard attributes present on every real leaf activity.
    ACTIVITY_DEFAULTS = {
        "visible":                 "True",
        "disabled":                "False",
        "isFavorite":              "False",
        "isJsonValid":             "True",
        "readPermission":          "True",
        "writePermission":         "True",
        "notes":                   "",
        "Timeout":                 "00:01:00",
        "TimeInSeconds":           "60",
        "RecoveryMethodSelection": "{x:Null}",
        "TargetModuleID":          "",
        "TargetModuleName":        "",
        "Path":                    "{x:Null}",
        "activityLicenseType":     "1",
        "IsValid":                 "True",
    }

    _id_lookup: dict | None = None

    # ------------------------------------------------------------------ #
    #  Namespace helpers                                                  #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _ns_prefix(activity_name: str) -> str:
        """
        Generate a deterministic, valid XML namespace prefix from an activity name.
        E.g. 'ADAddToGroup' → 'ns_adaddtogroup', 'PowerShellScript' → 'ns_powershellscript'
        """
        return "ns_" + re.sub(r'[^a-z0-9]', '', activity_name.lower())

    # ------------------------------------------------------------------ #
    #  Formula builder                                                    #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _build_formula(condition_type: str, value: str) -> str | None:
        if not condition_type:
            return None
        return f"={condition_type}(&&&,{value})"

    # ------------------------------------------------------------------ #
    #  Template id lookup                                                 #
    # ------------------------------------------------------------------ #

    def _load_id_lookup(self) -> dict:
        if self.__class__._id_lookup is not None:
            return self.__class__._id_lookup
        data_dir = os.getenv("DATA_DIR", "/app/data")
        path = os.path.join(data_dir, "activity_json_syntax.json")
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            templates = data.get("settings", data) if isinstance(data, dict) else data
            self.__class__._id_lookup = {
                t.get("CustomTypeName") or t.get("TypeName"): str(t["id"])
                for t in templates
                if t.get("id") and (t.get("CustomTypeName") or t.get("TypeName"))
            }
        except Exception:
            self.__class__._id_lookup = {}
        return self.__class__._id_lookup

    # ------------------------------------------------------------------ #
    #  Public entry point                                                 #
    # ------------------------------------------------------------------ #

    def compose(self, workflow_json: dict, workflow_name: str, pnumber: str) -> str:
        raw_data = workflow_json.get("workflow_raw_data", {})
        xoml = self._build_xoml(raw_data, workflow_name)
        now = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000")

        total_export = ET.Element("TotalExport", attrib={"sourceSystem": "NG"})

        workflows_elem = ET.SubElement(total_export, "Workflows")
        ET.SubElement(workflows_elem, "WorkflowInfo", attrib={
            "Pnumber":               pnumber,
            "Name":                  workflow_name,
            "Description":           workflow_json.get("description", ""),
            "Xoml":                  xoml,
            "XomlStatus":            "0",
            "Details":               "",
            "DateLic":               "",
            "WorkflowType":          "0",
            "WorkflowFolderId":      "0",
            "WorkflowParentId":      "0",
            "Permissions":           "",
            "ErrorHandling":         "",
            "CurrentRevisionNumber": "1",
            "DateCreated":           now,
            "DateCreatedUser":       "1",
            "DateModified":          now,
            "DateModifiedUser":      "1",
        })

        objects_elem = ET.SubElement(total_export, "Objects")
        for tag in [
            "Hosts", "ErrorHandlers", "ErrorMessages", "MessageTemplates",
            "Sites", "Developments", "Users", "Groups", "UsersGroupsArray",
            "Domains", "Commands", "Classifications", "Incidents", "TimeFrames",
            "Variables", "Modules", "Conditions", "ConditionArrays",
            "ConditionObjects", "SoapWebServices", "Triggers",
            "TriggerConditionArrays", "LogCategory", "LogTriggerCategory",
            "Schedules", "CustomActivities", "ActivitiesSource",
            "ScheduleCategoriesRelations",
        ]:
            ET.SubElement(objects_elem, tag)

        ET.SubElement(total_export, "ObjectsRelations")
        ET.SubElement(total_export, "ExportKeys")

        ET.indent(total_export, space="  ")
        return ET.tostring(total_export, encoding="unicode", xml_declaration=False)

    # ------------------------------------------------------------------ #
    #  XOML builder                                                       #
    # ------------------------------------------------------------------ #

    def _build_xoml(self, raw_data: dict, workflow_name: str) -> str:
        used_namespaces = self._collect_namespaces(raw_data)

        attribs = {
            "xmlns":   "http://schemas.microsoft.com/winfx/2006/xaml/workflow",
            "xmlns:x": "http://schemas.microsoft.com/winfx/2006/xaml",
            "x:Name":  "CustomWorkflow",
            "x:Class": "WorkflowDesignerControl.CustomWorkflow",
        }
        # Add one xmlns declaration per unique CLR namespace used
        for prefix, clr_string in used_namespaces.items():
            attribs[f"xmlns:{prefix}"] = clr_string

        root = ET.Element("SequentialWorkflowActivity", attrib=attribs)

        for xname, activity in raw_data.items():
            if isinstance(activity, dict):
                elem = self._serialize_activity(activity)
                if elem is not None:
                    root.append(elem)

        ET.indent(root, space="  ")
        return ET.tostring(root, encoding="unicode")

    def _collect_namespaces(self, node, seen=None):
        """
        Walk the workflow JSON and collect all CLR namespace strings needed.
        Returns { prefix: clr_string } for every non-None registry entry encountered.
        """
        if seen is None:
            seen = {}
        if isinstance(node, dict):
            ct = node.get("CustomTypeName", "")
            if ct in self.NAMESPACE_REGISTRY:
                clr_string = self.NAMESPACE_REGISTRY[ct]
                if clr_string is not None:
                    prefix = self._ns_prefix(ct)
                    if prefix not in seen:
                        seen[prefix] = clr_string
            for value in node.values():
                self._collect_namespaces(value, seen)
        return seen

    # ------------------------------------------------------------------ #
    #  Activity serializer                                                #
    # ------------------------------------------------------------------ #

    def _serialize_activity(self, activity: dict) -> ET.Element | None:
        custom_type = activity.get("CustomTypeName", "")
        if not custom_type:
            return None

        id_lookup = self._load_id_lookup()
        tag = self._resolve_tag(custom_type)
        attribs = {"x:Name": activity.get("xName", "")}
        child_elements = []

        for key, value in activity.items():
            if key in self.SKIP_FIELDS or key in ("xName", "CustomTypeName"):
                continue
            if isinstance(value, dict):
                child_elem = self._serialize_activity(value)
                if child_elem is not None:
                    child_elements.append(child_elem)
            elif isinstance(value, bool):
                attribs[key] = str(value)
            elif value is None:
                attribs[key] = "{x:Null}"
            elif isinstance(value, (int, float)):
                attribs[key] = str(value)
            else:
                attribs[key] = str(value)

        if custom_type not in self.NO_DEFAULTS:
            for k, v in self.ACTIVITY_DEFAULTS.items():
                if k not in attribs:
                    attribs[k] = v
            if "id" not in attribs and custom_type in id_lookup:
                attribs["id"] = id_lookup[custom_type]

            attribs["name"] = custom_type
            attribs["TypeName"] = custom_type
            attribs["DisplayName"] = custom_type
            attribs["label"] = custom_type

            if "Description" in attribs and "description" not in attribs:
                attribs["description"] = attribs["Description"]
            elif "description" not in attribs:
                attribs["description"] = ""

        if custom_type == "GetCellValue":
            attribs["ColumnType"] = "Name"

        if custom_type == "ReturnValue":
            condition_type = attribs.get("ConditionType", "")
            value = attribs.get("Value", "")
            formula = self._build_formula(condition_type, value)
            attribs["Formula"] = "{x:Null}" if formula is None else formula

        elem = ET.Element(tag, attrib=attribs)

        if custom_type in self.SEQUENCE_CONTAINERS:
            seq_elem = None
            other_children = []
            for child in child_elements:
                child_tag = child.tag.split(":")[-1] if ":" in child.tag else child.tag
                if child_tag == "SequenceActivity":
                    seq_elem = child
                else:
                    other_children.append(child)
            if seq_elem is not None:
                for child in other_children:
                    seq_elem.append(child)
                elem.append(seq_elem)
            else:
                for child in child_elements:
                    elem.append(child)
        else:
            for child in child_elements:
                elem.append(child)

        return elem

    def _resolve_tag(self, custom_type: str) -> str:
        """
        Returns the XML element tag for an activity.
        - Not in registry: plain tag (unknown activity, no prefix)
        - Registry value is None: plain tag (built-in or confirmed no-prefix)
        - Registry value is a CLR string: prefix:tag
        """
        if custom_type not in self.NAMESPACE_REGISTRY:
            return custom_type
        clr_string = self.NAMESPACE_REGISTRY[custom_type]
        if clr_string is None:
            return custom_type
        return f"{self._ns_prefix(custom_type)}:{custom_type}"
    