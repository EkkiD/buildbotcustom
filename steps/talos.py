import re, os, time, copy
from buildbot.steps.shell import WithProperties
from buildbot.status.builder import SUCCESS, WARNINGS, FAILURE, SKIPPED, EXCEPTION, worst_status

from buildbotcustom.steps.base import ShellCommand

class MozillaUpdateConfig(ShellCommand):
    """Configure YAML file for run_tests.py"""
    name = "Update config"

    def __init__(self, branch, branchName, executablePath,
            addOptions=None, useSymbols=False, addonTester=False,
            remoteTests=False, extName=None, remoteExtras=None,
            remoteProcessName=None, **kwargs):

        if addOptions is None:
            self.addOptions = []
        else:
            self.addOptions = addOptions[:]

        if remoteExtras is not None:
            self.remoteExtras = remoteExtras
        else:
            self.remoteExtras = {}

        self.remoteProcessName = remoteProcessName
        self.remoteOptions = self.remoteExtras.get('options', [])

        self.branch = branch
        self.branchName = branchName
        self.exePath = executablePath
        self.useSymbols = useSymbols
        self.extName = extName
        self.addonTester = addonTester
        self.remoteTests = remoteTests

        self.super_class = ShellCommand
        self.super_class.__init__(self, **kwargs)

        self.addFactoryArguments(branch=branch, addOptions=addOptions,
                branchName=branchName, executablePath=executablePath,
                remoteTests=remoteTests, useSymbols=useSymbols,
                extName=extName, addonTester=addonTester,
                remoteExtras=self.remoteExtras,
                remoteProcessName=remoteProcessName)

    def setBuild(self, build):
        self.super_class.setBuild(self, build)
        title = build.slavename

        try:
            self.addOptions += self.getProperty('configurationOptions').split(',')
        # Property doesn't exist, that's fine
        except KeyError:
            pass

        #if we are an addonTester then the addonName/addonUrl build property should be set
        #  if it's not set this will throw a key error and the run will go red - which should be the expected result
        if self.addonTester:
            addon_id = os.path.basename(self.build.getProperty('addonUrl')) #eg /en-US/firefox/downloads/latest/10900
            if not addon_id.isdigit(): #eg http://ftp.mozilla.org/pub/mozilla.org/addons/92382/ie_tab_2__ff_3.6+_-2.12.21.1-fx.xpi
                addon_id = (self.getProperty('addonUrl')).split('/')[-2]
            self.addOptions += ['--addonID', addon_id, '--extension', self.extName]

        if self.useSymbols:
            self.addOptions += ['--symbolsPath', '../symbols']

        if self.remoteTests:
            exePath = self.remoteProcessName
            perfconfigurator = "remotePerfConfigurator.py"
            self.addOptions += ['--remoteDevice', WithProperties('%(sut_ip)s'), ]
            self.addOptions += self.remoteOptions
        else:
            exePath = self.exePath
            perfconfigurator = "PerfConfigurator.py"

        self.setCommand(["python", perfconfigurator, "-v", "-e", exePath,
            "-t", title, '--branchName', self.branchName] + self.addOptions)

    def evaluateCommand(self, cmd):
        superResult = self.super_class.evaluateCommand(self, cmd)
        if SUCCESS != superResult:
            return superResult
        stdioText = cmd.logs['stdio'].getText()
        if None != re.search('ERROR', stdioText):
            return FAILURE
        if None != re.search('USAGE:', stdioText):
            return FAILURE
        configFileMatch = re.search('outputName\s*=\s*(\w*?.yml)', stdioText)
        if not configFileMatch:
            return FAILURE
        else:
            self.setProperty("configFile", configFileMatch.group(1))
        return SUCCESS

class MozillaRunPerfTests(ShellCommand):
    """Run the performance tests"""
    name = "Run performance tests"
    def __init__(self, **kwargs):
        self.super_class = ShellCommand
        self.super_class.__init__(self, **kwargs)

    def createSummary(self, log):
        summary = []
        for line in log.readlines():
            if "RETURN:" in line:
                summary.append(line.replace("RETURN:", "TinderboxPrint:"))
            if "FAIL:" in line:
                summary.append(line.replace("FAIL:", "TinderboxPrint:FAIL:"))
        self.addCompleteLog('summary', "\n".join(summary))

    def evaluateCommand(self, cmd):
        superResult = self.super_class.evaluateCommand(self, cmd)
        stdioText = cmd.logs['stdio'].getText()
        if SUCCESS != superResult:
            return superResult
        if None != re.search('ERROR', stdioText):
            return FAILURE
        if None != re.search('USAGE:', stdioText):
            return FAILURE
        if None != re.search('FAIL:', stdioText):
            return WARNINGS
        return SUCCESS
