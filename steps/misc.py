# ***** BEGIN LICENSE BLOCK *****
# Version: MPL 1.1
#
# The contents of this file are subject to the Mozilla Public License Version
# 1.1 (the "License"); you may not use this file except in compliance with
# the License. You may obtain a copy of the License at
# http://www.mozilla.org/MPL/
#
# Software distributed under the License is distributed on an "AS IS" basis,
# WITHOUT WARRANTY OF ANY KIND, either express or implied. See the License
# for the specific language governing rights and limitations under the
# License.
#
# The Original Code is Mozilla-specific Buildbot steps.
#
# The Initial Developer of the Original Code is
# Mozilla Corporation.
# Portions created by the Initial Developer are Copyright (C) 2007
# the Initial Developer. All Rights Reserved.
#
# Contributor(s):
#   Ben Hearsum <bhearsum@mozilla.com>
#   Rob Campbell <rcampbell@mozilla.com>
#   Chris Cooper <ccooper@mozilla.com>
# ***** END LICENSE BLOCK *****

from twisted.python.failure import Failure, DefaultException
from twisted.internet import reactor
from twisted.spread.pb import PBConnectionLost
from twisted.python import log
from twisted.internet.defer import Deferred, TimeoutError

import os
import re

from buildbot.process.buildstep import LoggedRemoteCommand, BuildStep
from buildbot.steps.shell import WithProperties
from buildbot.status.builder import FAILURE, SUCCESS, worst_status
from buildbot.status.builder import STDOUT, STDERR #ScratchboxProperty

from buildbotcustom.steps.base import LoggingBuildStep, ShellCommand, \
  addRetryEvaluateCommand, RetryingShellCommand
from buildbotcustom.common import genBuildID, genBuildUID
from buildbotcustom.try_parser import processMessage

def errbackAfter(wrapped_d, timeout):
    # Thanks to Dustin!
    """Calls wrapped_d's errback after timeout seconds"""
    wrapper_d = Deferred()
    already_fired = [False]
    def cb(*args, **kwargs):
        if not already_fired[0]:
            already_fired[0] = True
            wrapper_d.callback(*args, **kwargs)
        else:
            log.msg("callback called again: %s %s" % (args, kwargs))
    def eb(*args, **kwargs):
        if not already_fired[0]:
            already_fired[0] = True
            wrapper_d.errback(*args, **kwargs)
        else:
            log.msg("errback called again: %s %s" % (args, kwargs))
    def to():
        if not already_fired[0]:
            already_fired[0] = True
            wrapper_d.errback(TimeoutError("More than %i seconds elapsed" % timeout))
    reactor.callLater(timeout, to)
    wrapped_d.addCallbacks(cb, eb)
    return wrapper_d

class InterruptableDeferred(Deferred):
    def __init__(self, wrapped_d):
        Deferred.__init__(self)

        self.already_fired = False

        def callback(*args, **kwargs):
            if not self.already_fired:
                self.already_fired = True
                self.callback(*args, **kwargs)
            else:
                log.msg("callback called again: %s %s" % (args, kwargs))

        def errback(*args, **kwargs):
            if not self.already_fired:
                self.already_fired = True
                self.errback(*args, **kwargs)
            else:
                log.msg("errback called again: %s %s" % (args, kwargs))

        wrapped_d.addCallbacks(callback, errback)

    def interrupt(self, reason="Interrupted"):
        if not self.already_fired:
            self.already_fired = True
            self.errback(DefaultException(reason))


class ScratchboxCommand(ShellCommand):
    #Note: this class doesn't deal with all WithProperties invocations.
    #in particular, it only deals with the WithProperties('format string')
    #case
    # Things to address:
    #   -what happens if the workdir doesn't exist?
    #     -only issue if this is the first command called in a build
    #   -should add reconfig/checkconfig time check for valid workdir (no spaces!)
    #   -doesn't currently set properties
    #   -magic function should be implemented as a mixin or something

    def __init__(self, sb=False, sb_login='/scratchbox/moz_scratchbox',
                 sb_workdir_mutator=lambda x: x, sb_args=['-p', '-k'],
                 **kwargs):
        self.super_class = ShellCommand
        self.super_class.__init__(self,**kwargs)
        self.sb = sb
        self.sb_login = sb_login
        self.sb_workdir_mutator = sb_workdir_mutator
        self.sb_args = sb_args
        assert 'workdir' in kwargs.keys(), "You *must* specify workdir"
        self.addFactoryArguments(sb=sb,
                                 sb_login=sb_login,
                                 sb_workdir_mutator=sb_workdir_mutator,
                                 sb_args=sb_args,
                                )

    def magic(self):
        #This variable is used to decide whether to wrap the 
        #command in a WithProperties instance
        use_with_properties = False

        #We need to have all commands as a string.  We'll
        #convert argv commands into string commands
        if isinstance(self.command, list):
            string_list = []
            for arg in self.command:
                if issubclass(arg.__class__, WithProperties):
                    use_with_properties = True
                    string_list.append(arg.fmtstring)
                else:
                    string_list.append(arg)
            string_command = ' '.join([str(x) for x in string_list])
        elif issubclass(self.command.__class__, WithProperties):
            use_with_properties = True
            string_command = self.command.fmtstring
        else:
            string_command = self.command
        sb_workdir = self.sb_workdir_mutator(self.remote_kwargs['workdir'])

        #If the workdir is a WithProperties instance, we need to get the format
        #string and wrap it in another WithProperties
        if issubclass(sb_workdir.__class__, WithProperties):
            use_with_properties = True
            sb_workdir = sb_workdir.fmtstring

        assert ' ' not in sb_workdir, 'scratchbox cannot deal with spaces in workdir'

        full_command = r'%s %s -d %s %s' % (self.sb_login,
                                                ' '.join(self.sb_args),
                                                sb_workdir,
                                                string_command)

        if use_with_properties:
            self.command = WithProperties(full_command)
        else:
            self.command = full_command


    def start(self):
        if self.sb:
            self.magic()
        self.super_class.start(self)

class ScratchboxProperty(ScratchboxCommand):
    # This class could be implemented cleaner by implementing
    # the scratchbox logic differently.  Patches accepted
    name = "scratchbox-setproperty"

    def __init__(self, property=None, extract_fn=None, strip=True, **kwargs):
        self.property = property
        self.extract_fn = extract_fn
        self.strip = strip

        assert (property is not None) ^ (extract_fn is not None), \
                "Exactly one of property and extract_fn must be set"

        self.super_class = ScratchboxCommand
        self.super_class.__init__(self, **kwargs)

        self.addFactoryArguments(property=self.property)
        self.addFactoryArguments(extract_fn=self.extract_fn)
        self.addFactoryArguments(strip=self.strip)

        self.property_changes = {}

    def commandComplete(self, cmd):
        if self.property:
            result = cmd.logs['stdio'].getText()
            if self.strip: result = result.strip()
            propname = self.build.getProperties().render(self.property)
            self.setProperty(propname, result, "ScratchboxProperty Step")
            self.property_changes[propname] = result
        else:
            log = cmd.logs['stdio']
            new_props = self.extract_fn(cmd.rc,
                    ''.join(log.getChunks([STDOUT], onlyText=True)),
                    ''.join(log.getChunks([STDERR], onlyText=True)))
            for k,v in new_props.items():
                self.setProperty(k, v, "ScratchboxProperty Step")
            self.property_changes = new_props

    def createSummary(self, log):
        props_set = [ "%s: %r" % (k,v) for k,v in self.property_changes.items() ]
        self.addCompleteLog('property changes', "\n".join(props_set))

    def getText(self, cmd, results):
        if self.property_changes:
            return [ "set props:" ] + self.property_changes.keys()
        else:
            return [ "no change" ]

RetryingScratchboxProperty = addRetryEvaluateCommand(ScratchboxProperty)

class CreateDir(ShellCommand):
    name = "create dir"
    haltOnFailure = False
    warnOnFailure = True

    def __init__(self, platform, dir=None, **kwargs):
        self.super_class = ShellCommand
        self.super_class.__init__(self, **kwargs)
        self.addFactoryArguments(platform=platform, dir=dir)
        self.platform = platform
        if dir:
            self.dir = dir
        else:
            if self.platform.startswith('win'):
                self.command = r'if not exist ' + self.dir + r' mkdir ' + \
                               self.dir
            else:
                self.command = ['mkdir', '-p', self.dir]

class TinderboxShellCommand(ShellCommand):
    haltOnFailure = False

    """This step is really just a 'do not care' buildstep for executing a
       slave command and ignoring the results. If ignoreCodes is passed,
       only exit codes listed in it will be ignored. If ignoreCodes is not
       passed, all exit codes will be ignored.
    """
    def __init__(self, ignoreCodes=None, **kwargs):
       self.super_class = ShellCommand
       self.super_class.__init__(self, **kwargs)
       self.addFactoryArguments(ignoreCodes=ignoreCodes)
       self.ignoreCodes = ignoreCodes

    def evaluateCommand(self, cmd):
       # Ignore all return codes
       if not self.ignoreCodes:
          return SUCCESS
       else:
          # Ignore any of the return codes we're told to
          if cmd.rc in self.ignoreCodes:
             return SUCCESS
          # If the return code is something else, fail
          else:
             return FAILURE

class GetBuildID(ShellCommand):
    """Retrieves the BuildID from a Mozilla tree (using platform.ini) and sets
    it as a build property ('buildid'). If defined, uses objdir as it's base.
    """
    description=['getting buildid']
    descriptionDone=['get buildid']
    haltOnFailure=True

    def __init__(self, objdir="", inifile="application.ini", section="App",
            **kwargs):
        self.super_class = ShellCommand
        self.super_class.__init__(self, **kwargs)
        self.addFactoryArguments(objdir=objdir,
                                 inifile=inifile,
                                 section=section)

        self.objdir = objdir
        self.command = ['python', 'config/printconfigsetting.py',
                        '%s/dist/bin/%s' % (self.objdir, inifile),
                        section, 'BuildID']

    def commandComplete(self, cmd):
        buildid = ""
        try:
            buildid = cmd.logs['stdio'].getText().strip().rstrip()
            self.setProperty('buildid', buildid)
        except:
            log.msg("Could not find BuildID or BuildID invalid")
            log.msg("Found: %s" % buildid)
            return FAILURE
        return SUCCESS


class SetMozillaBuildProperties(LoggingBuildStep):
    """Gathers and sets build properties for the following data:
      buildid - BuildID of the build (from application.ini, falling back on
       platform.ini)
      appVersion - The version of the application (from application.ini, falling
       back on platform.ini)
      packageFilename - The filename of the application package
      packageSize - The size (in bytes) of the application package
      packageHash - The sha1 hash of the application package
      installerFilename - The filename of the installer (win32 only)
      installerSize - The size (in bytes) of the installer (win32 only)
      installerHash - The sha1 hash of the installer (win32 only)
      completeMarFilename - The filename of the complete update
      completeMarSize - The size (in bytes) of the complete update
      completeMarHash - The sha1 hash of the complete update

      All of these will be set as build properties -- even if no data is found
      for them. When no data is found, the value of the property will be None.

      This function requires an argument of 'objdir', which is the path to the
      objdir relative to the builddir. ie, 'mozilla/fx-objdir'.
    """

    def __init__(self, objdir="", **kwargs):
        LoggingBuildStep.__init__(self, **kwargs)
        self.addFactoryArguments(objdir=objdir)
        self.objdir = objdir

    def describe(self, done=False):
        if done:
            return ["gather", "build", "properties"]
        else:
            return ["gathering", "build", "properties"]

    def start(self):
        args = {'objdir': self.objdir, 'timeout': 60}
        cmd = LoggedRemoteCommand("setMozillaBuildProperties", args)
        self.startCommand(cmd)

    def evaluateCommand(self, cmd):
        # set all of the data as build properties
        # some of this may come in with the value 'UNKNOWN' - these will still
        # be set as build properties but 'UNKNOWN' will be substituted with None
        try:
            log = cmd.logs['stdio'].getText()
            for property in log.split("\n"):
                name, value = property.split(": ")
                if value == "UNKNOWN":
                    value = None
                self.setProperty(name, value)
        except:
            return FAILURE
        return SUCCESS

class SendChangeStep(ShellCommand):
    warnOnFailure = True
    flunkOnFailure = False
    name = "sendchange"
    description = ["sendchange"]

    def __init__(self, master, branch, files, revision=None, user=None,
                 comments="", sendchange_props=None, timeout=1800, retries=5, **kwargs):

        self.super_class = ShellCommand
        self.super_class.__init__(self, **kwargs)
        self.addFactoryArguments(master=master, branch=branch, files=files,
                                 revision=revision, user=user,
                                 comments=comments, timeout=timeout,
                                 sendchange_props=sendchange_props, retries=retries)
        self.master = master
        self.branch = branch
        self.files = files
        self.revision = revision
        self.user = user
        self.comments = comments
        self.sendchange_props = sendchange_props or {}
        self.timeout = timeout
        self.retries = retries

        self.name = 'sendchange'

        self.sleepTime = 5

    def start(self):
        try:
            props = self.build.getProperties()
            branch = props.render(self.branch)
            revision = props.render(self.revision)
            comments = props.render(self.comments)
            files = props.render(self.files)
            user = props.render(self.user)
            sendchange_props = []
            for key, value in self.sendchange_props.items():
                sendchange_props.append( (key, props.render(value)) )

            self.addCompleteLog("sendchange", """\
    master: %s
    branch: %s
    revision: %s
    comments: %s
    user: %s
    files: %s
    properties: %s""" % (self.master, branch, revision, comments,
                         user, files, sendchange_props))
            bb_cmd = ['buildbot', 'sendchange', '--master', self.master,
                      '--username', user, '--branch', branch,
                      '--revision', revision]
            if isinstance(comments, basestring):
                if re.search('try: ', comments, re.MULTILINE):
                    comments = 'try: ' + ' '.join(processMessage(comments))
                else:
                    try:
                        comments = comments.splitlines()[0]
                    except IndexError:
                        comments = ''
                comments = re.sub(r'[\r\n^<>|;&"\'%$]', '_', comments)
                comments = comments.encode('ascii', 'replace')
                if comments:
                    bb_cmd.extend(['--comments', comments])

            for key, value in sendchange_props:
                bb_cmd.extend(['--property', '%s:%s' % (key, value)])

            if files:
                bb_cmd.extend(self.files)

            cmd = ['python',
                   WithProperties("%(toolsdir)s/buildfarm/utils/retry.py"),
                   '-s', str(self.sleepTime), '-t', str(self.timeout),
                   '-r', str(self.retries), '--stdout-regexp', 'change sent successfully']
            cmd.extend(bb_cmd)
            self.setCommand(cmd)
            self.super_class.start(self)
        except KeyError:
            self.addCompleteLog("errors", str(Failure()))
            return self.finished(FAILURE)


class DownloadFile(ShellCommand):
    haltOnFailure = True
    name = "download"
    description = ["download"]

    def __init__(self, url_fn=None, url=None, url_property=None, filename_property=None,
            ignore_certs=False, wget_args=None, **kwargs):
        self.url = url
        self.url_fn = url_fn
        self.url_property = url_property
        self.filename_property = filename_property
        self.ignore_certs = ignore_certs
        assert bool(self.url) ^ bool(self.url_fn), \
                "One of url_fn or url must be set, not both (%s %s)"
        if wget_args:
            self.wget_args = wget_args
        else:
            self.wget_args = ['--progress=dot:mega']
        self.super_class = ShellCommand
        self.super_class.__init__(self, **kwargs)
        self.addFactoryArguments(url_fn=url_fn, url=url,
                url_property=url_property, filename_property=filename_property,
                ignore_certs=ignore_certs, wget_args=wget_args)

    def start(self):
        try:
            if self.url_fn:
                url = self.url_fn(self.build)
            else:
                url = self.url
        except Exception, e:
            self.addCompleteLog("errors", "Automation Error: %s" % str(e))
            return self.finished(FAILURE)

        renderedUrl = self.build.getProperties().render(url)
        if self.url_property:
            self.setProperty(self.url_property, renderedUrl, "DownloadFile")
        if self.filename_property:
            self.setProperty(self.filename_property,
                    os.path.basename(renderedUrl), "DownloadFile")

        if self.ignore_certs:
            self.setCommand(["wget"] + self.wget_args + ["-N", "--no-check-certificate", url])
        else:
            self.setCommand(["wget"] + self.wget_args + ["-N", url])
        self.super_class.start(self)

    def evaluateCommand(self, cmd):
        superResult = self.super_class.evaluateCommand(self, cmd)
        if SUCCESS != superResult:
            return superResult
        if None != re.search('ERROR', cmd.logs['stdio'].getText()):
            return FAILURE
        return SUCCESS

class UnpackFile(ShellCommand):
    description = ["unpack"]

    def __init__(self, filename, scripts_dir=".", **kwargs):
        self.filename = filename
        self.scripts_dir = scripts_dir
        self.super_class = ShellCommand
        self.super_class.__init__(self, **kwargs)
        self.addFactoryArguments(filename=filename, scripts_dir=scripts_dir)

    def start(self):
        filename = self.build.getProperties().render(self.filename)
        self.filename = filename
        if filename.endswith(".zip") or filename.endswith(".apk"):
            self.setCommand(['unzip', '-o', filename])
        elif filename.endswith(".tar.gz"):
            self.setCommand(['tar', '-zxvf', filename])
        elif filename.endswith(".tar.bz2"):
            self.setCommand(['tar', '-jxvf', filename])
        elif filename.endswith(".dmg"):
            self.setCommand(['bash',
             '%s/installdmg.sh' % self.scripts_dir,
             filename]
            )
        else:
            raise ValueError("Don't know how to handle %s" % filename)
        self.super_class.start(self)

    def evaluateCommand(self, cmd):
        superResult = self.super_class.evaluateCommand(self, cmd)
        if SUCCESS != superResult:
            return superResult
        if None != re.search('^Usage:', cmd.logs['stdio'].getText()):
            return FAILURE

        return SUCCESS

class UnpackTest(ShellCommand):
    description = ["unpack", "tests"]

    def __init__(self, filename, testtype, scripts_dir=".", **kwargs):
        self.super_class = ShellCommand
        self.super_class.__init__(self, **kwargs)
        self.filename = filename
        self.scripts_dir = scripts_dir
        self.testtype = testtype
        self.addFactoryArguments(filename=filename, testtype=testtype, scripts_dir=scripts_dir)

    def start(self):
        filename = self.build.getProperties().render(self.filename)
        self.filename = filename
        if filename.endswith(".zip"):
            # modify the commands to extract only the files we need - the test directory and bin/ and certs/
            if self.testtype == "mochitest":
                self.setCommand(['unzip', '-o', filename, 'bin*', 'certs*', 'mochitest*'])
            elif self.testtype == "xpcshell":
                self.setCommand(['unzip', '-o', filename, 'bin*', 'certs*', 'xpcshell*'])
            elif self.testtype == "jsreftest":
                # jsreftest needs both jsreftest/ and reftest/ in addition to bin/ and certs/
                self.setCommand(['unzip', '-o', filename, 'bin*', 'certs*', 'jsreftest*', 'reftest*'])
            elif self.testtype == "reftest":
                self.setCommand(['unzip', '-o', filename, 'bin*', 'certs*', 'reftest*'])
            elif self.testtype == "jetpack":
                self.setCommand(['unzip', '-o', filename, 'bin*', 'certs*', 'jetpack*'])
            else:
                # If it all fails, we extract the whole shebang
                self.setCommand(['unzip', '-o', filename])
        #If we come across a test not packaged as a zip file, try unpacking the whole thing using tar+gzip/bzip2
        elif filename.endswith("tar.bz2"):
            self.setCommand(['tar', '-jxvf', filename])
        elif filename.endswith("tar.gz"):
            self.setCommand(['tar', '-zxvf', filename])
        else:
            # TODO: The test package is .zip across all three platforms, so we're special casing for that
            raise ValueError("Don't know how to handle %s" % filename)
        self.super_class.start(self)

    def evaluateCommand(self, cmd):
        superResult = self.super_class.evaluateCommand(self, cmd)
        if superResult != SUCCESS:
            return superResult
        if None != re.search('^Usage:', cmd.logs['stdio'].getText()):
            return FAILURE

        return SUCCESS

class FindFile(ShellCommand):
    def __init__(self, filename, directory, max_depth, property_name, filetype=None, **kwargs):
        self.super_class = ShellCommand
        self.super_class.__init__(self, **kwargs)

        self.addFactoryArguments(filename=filename, directory=directory,
                max_depth=max_depth, property_name=property_name,
                filetype=filetype)

        self.property_name = property_name

        if filetype == "file":
            filetype = "-type f"
        elif filetype == "dir":
            filetype = "-type d"
        else:
            filetype = ""

        self.setCommand(['bash', '-c', 'find %(directory)s -maxdepth %(max_depth)s %(filetype)s -name %(filename)s' % locals()])

    def evaluateCommand(self, cmd):
        worst = self.super_class.evaluateCommand(self, cmd)
        try:
            output = cmd.logs['stdio'].getText().strip()
            if output:
                self.setProperty(self.property_name, output)
                worst = worst_status(worst, SUCCESS)
            else:
                worst = worst_status(worst, FAILURE)
        except:
            pass
        return worst

class MozillaClobberer(ShellCommand):
    flunkOnFailure = False
    description=['checking', 'clobber', 'times']

    def __init__(self, branch, clobber_url, clobberer_path, clobberTime=None,
                 timeout=3600, workdir='..', command=[], **kwargs):
        command = ['python', clobberer_path, '-s', 'tools']
        if clobberTime:
            command.extend(['-t', str(clobberTime)])

        command.extend([
            clobber_url,
            branch,
            WithProperties("%(buildername)s"),
            WithProperties("%(slavebuilddir)s"),
            WithProperties("%(slavename)s"),
            WithProperties("%(master)s"),
        ])

        self.super_class = ShellCommand

        self.super_class.__init__(self, command=command, timeout=timeout,
                              workdir=workdir, **kwargs)

        self.addFactoryArguments(branch=branch, clobber_url=clobber_url,
                                 clobberer_path=clobberer_path,
                                 clobberTime=clobberTime)

    def setBuild(self, build):
        self.super_class.setBuild(self, build)
        # Set the "master" property
        master = build.builder.botmaster.parent.buildbotURL
        self.setProperty('master', master)

    def createSummary(self, log):
        my_builder = self.getProperty("builddir")
        # Server is forcing a clobber
        forcedClobberRe = re.compile('%s:Server is forcing a clobber' % my_builder)
        # We are looking for something like :
        #  More than 604800.0 seconds have passed since our last clobber
        periodicClobberRe = re.compile('%s:More than [\d+\.]+ seconds have passed since our last clobber' % my_builder)

        # We don't have clobber data.  This usually means we've been purged before
        purgedClobberRe = re.compile("%s:Our last clobber date:.*None" % my_builder)

        self.setProperty('forced_clobber', False, 'MozillaClobberer')
        self.setProperty('periodic_clobber', False, 'MozillaClobberer')
        self.setProperty('purged_clobber', False, 'MozillaClobberer')

        clobberType = None
        for line in log.readlines():
            if forcedClobberRe.search(line):
                self.setProperty('forced_clobber', True, 'MozillaClobberer')
                clobberType = "forced"
            elif periodicClobberRe.search(line):
                self.setProperty('periodic_clobber', True, 'MozillaClobberer')
                clobberType = "periodic"
            elif purgedClobberRe.search(line):
                self.setProperty('purged_clobber', True, 'MozillaClobberer')
                clobberType = "free-space"

        if clobberType != None:
            summary = "TinderboxPrint: %s clobber" % clobberType
            self.addCompleteLog('clobberer', summary)

class SetBuildProperty(BuildStep):
    name = "set build property"
    def __init__(self, property_name, value, **kwargs):
        self.property_name = property_name
        self.value = value

        BuildStep.__init__(self, **kwargs)

        self.addFactoryArguments(property_name=property_name, value=value)

    def start(self):
        if callable(self.value):
            value = self.value(self.build)
        else:
            value = self.value
        self.setProperty(self.property_name, value)
        self.step_status.setText(['set props:', self.property_name])
        self.addCompleteLog("property changes", "%s: %s" % (self.property_name, value))
        return self.finished(SUCCESS)

class OutputStep(BuildStep):
    """Simply logs some output"""
    name = "output"
    def __init__(self, data, log='output', **kwargs):
        self.data = data
        self.log = log

        BuildStep.__init__(self, **kwargs)

        self.addFactoryArguments(data=data, log=log)

    def start(self):
        properties = self.build.getProperties()
        if callable(self.data):
            data = properties.render(self.data(self.build))
        else:
            data = properties.render(self.data)
        if not isinstance(data, (str, unicode)):
            try:
                data = " ".join(data)
            except:
                data = str(data)
        self.addCompleteLog(self.log, data)
        self.step_status.setText([self.name])
        return self.finished(SUCCESS)

class DisconnectStep(ShellCommand):
    """This step is used when a command is expected to cause the slave to
    disconnect from the master.  It will handle connection lost errors as
    expected.

    Optionally it will also forcibly disconnect the slave from the master by
    calling the remote 'shutdown' command, in effect doing a graceful
    shutdown.  If force_disconnect is True, then the slave will always be
    disconnected after the command completes.  If force_disconnect is a
    function, it will be called with the command object, and the return value
    will be used to determine if the slave should be disconnected."""
    name = "disconnect"
    def __init__(self, force_disconnect=None, **kwargs):
        self.force_disconnect = force_disconnect
        self.super_class = ShellCommand
        self.super_class.__init__(self, **kwargs)
        self.addFactoryArguments(force_disconnect=force_disconnect)

        self._disconnected = False
        self._deferred_death = None

    def interrupt(self, reason):
        # Called when the slave command is interrupted, e.g. by rebooting
        # We assume this is expected
        self._disconnected = True
        return self.finished(SUCCESS)

    def start(self):
        # Give the machine 60 seconds to go away on its own
        def die():
            self._deferred_death = None
            log.msg("Forcibly disconnecting %s" % self.getSlaveName())
            self.buildslave.disconnect()
        self._deferred_death = reactor.callLater(60, die)
        return self.super_class.start(self)

    def checkDisconnect(self, f):
        # This is called if there's a problem executing the command because the connection was disconnected.
        # Again, we assume this is the expected behaviour
        f.trap(PBConnectionLost)
        self._disconnected = True
        return self.finished(SUCCESS)

    def commandComplete(self, cmd):
        # The command has completed normally.  If force_disconnect is set, then
        # tell the slave to shutdown
        if self.force_disconnect:
            if not callable(self.force_disconnect) or self.force_disconnect(cmd):
                try:
                    d = self.remote.callRemote('shutdown')
                    d.addErrback(self._disconnected_cb)
                    d.addCallback(self._disconnected_cb)
                    return d
                except:
                    log.err()
                    return

        # Otherwise, cancel our execution
        if self._deferred_death and self._deferred_death.active:
            self._deferred_death.cancel()
            self._deferred_death = None

    def _disconnected_cb(self, res):
        # Successfully disconnected
        self._disconnected = True
        return True

    def finished(self, res):
        if self._disconnected:
            self.step_status.setText(self.describe(True) + ["slave", "lost"])
            self.step_status.setText2(['slave', 'lost'])
            if self._deferred_death and self._deferred_death.active:
                self._deferred_death.cancel()
                self._deferred_death = None
        return self.super_class.finished(self, res)

class RepackPartners(ShellCommand):
    '''This step allows a regular ShellCommand to be optionally extended
       based on provided properties. This is useful for tweaking the command
       to be run based on, e.g., properties supplied by the user in the
       force builds web interface.
    '''
    def __init__(self, **kwargs):
        self.super_class = ShellCommand
        self.super_class.__init__(self, **kwargs)
    def start(self):
        try:
            properties = self.build.getProperties()
            if properties.has_key('partner'):
                partner = properties['partner']
                self.command.extend(['-p',partner])
        except:
            # No partner was specified, so repacking all partners.
            pass
        self.super_class.start(self)

class FunctionalStep(BuildStep):
    name = "functional_step"
    def __init__(self, func, **kwargs):
        self.func = func

        BuildStep.__init__(self, **kwargs)

        self.addFactoryArguments(func=func)

    def start(self):
        result = self.func(self, self.build)
        return self.finished(result)

def setBuildIDProps(step, build):
    """Sets buildid and builduid properties.

    On a rebuild we willl re-generate the builduid.  Otherwise, we normally get
    them from the scheduler.

    If either of buildid or builduid doesn't exist, it will be created."""

    if build.reason.startswith("The web-page 'rebuild'"):
        # Override builduid since this is a manually triggered
        # rebuild
        build.setProperty("builduid", genBuildUID(), "setBuildProps")

    # Make sure we have required properties
    props = build.getProperties()
    if not props.has_key("buildid"):
        build.setProperty("buildid", genBuildID(), "setBuildProps")
    if not props.has_key("builduid"):
        build.setProperty("builduid", genBuildUID(), "setBuildProps")

    return SUCCESS
