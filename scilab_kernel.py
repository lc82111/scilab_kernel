from IPython.kernel.zmq.kernelbase import Kernel
from IPython.utils.path import locate_profile
from scilab2py import Scilab2PyError, scilab

import os
import signal
from subprocess import check_output, CalledProcessError
import re
import logging

__version__ = '0.1'

version_pat = re.compile(r'version "(\d+(\.\d+)+)')


# TODO: allow inline plotting

class ScilabKernel(Kernel):
    implementation = 'scilab_kernel'
    implementation_version = __version__
    language = 'scilab'

    @property
    def language_version(self):
        self.log.info(self.banner)
        m = version_pat.search(self.banner)
        return m.group(1)

    _banner = None

    @property
    def banner(self):
        if self._banner is None:
            try:
                banner = check_output(['scilab',
                                       '-version']).decode('utf-8')
            except CalledProcessError as e:
                banner = e.output
            self._banner = banner
        return self._banner

    def __init__(self, **kwargs):
        Kernel.__init__(self, **kwargs)

        self.log.setLevel(logging.INFO)

        # Signal handlers are inherited by forked processes,
        # and we can't easily reset it from the subprocess.
        # Since kernelapp ignores SIGINT except in message handlers,
        # we need to temporarily reset the SIGINT handler here
        # so that octave and its children are interruptible.
        sig = signal.signal(signal.SIGINT, signal.SIG_DFL)
        try:
            self.scilab_wrapper = scilab
            scilab.restart()
            # this forces scilab to start up prior to the kernel
            # we need this var because `_` is a function in Scilab
            self.scilab_wrapper.put('last_kernel_value', '')
        finally:
            signal.signal(signal.SIGINT, sig)

        try:
            self.hist_file = os.path.join(locate_profile(),
                                          'scilab_kernel.hist')
        except IOError:
            self.hist_file = None
            self.log.warn('No default profile found, history unavailable')

        self.max_hist_cache = 1000
        self.hist_cache = []

    def do_execute(self, code, silent, store_history=True,
                   user_expressions=None, allow_stdin=False):
        """Execute a line of code in Octave."""
        code = code.strip()
        abort_msg = {'status': 'abort',
                     'execution_count': self.execution_count}

        if code and store_history:
            self.hist_cache.append(code)

        if not code or code == 'keyboard' or code.startswith('keyboard('):
            return {'status': 'ok', 'execution_count': self.execution_count,
                    'payload': [], 'user_expressions': {}}

        elif (code == 'exit' or code.startswith('exit(')
                or code == 'quit' or code.startswith('quit(')):
            # TODO: exit gracefully here
            self.do_shutdown(False)
            return abort_msg

        elif code == 'restart' or code.startswith('restart('):
            self.scilab_wrapper.restart()
            return abort_msg

        elif code.endswith('?') or code.startswith('?'):
            self._get_help(code)
            return abort_msg

        elif '_' in code:

            def fill_value(match):
                string = match.string[match.start():match.end()]
                return string.replace('_', 'last_kernel_value')

            code = re.sub('\W_\W|\W_\Z|\A_\W|\A_\Z', fill_value, code)

        interrupted = False
        try:
            output = self.scilab_wrapper._eval([code])

        except KeyboardInterrupt:
            self.scilab_wrapper._session.proc.send_signal(signal.SIGINT)
            interrupted = True
            output = 'Scilab Session Interrupted'

        except Scilab2PyError as e:
            return self._handle_error(str(e))

        except Exception:
            self.scilab_wrapper.restart()
            output = 'Uncaught Exception, Restarting Scilab'

        else:
            output = self._handle_output(output, silent)
            if output == 'Scilab Session Interrupted':
                interrupted = True

        if interrupted:
            return abort_msg

        return {'status': 'ok', 'execution_count': self.execution_count,
                'payload': [], 'user_expressions': {}}

    def do_complete(self, code, cursor_pos):
        """Get code completions using Scilab's ``completions``"""
        code = code[:cursor_pos]
        default = {'matches': [], 'cursor_start': 0,
                   'cursor_end': cursor_pos, 'metadata': dict(),
                   'status': 'ok'}

        if code[-1] == ' ':
            return default

        tokens = code.replace(';', ' ').split()
        if not tokens:
            return default
        token = tokens[-1]

        if os.sep in token:
            dname = os.path.dirname(token)
            rest = os.path.basename(token)

            if os.path.exists(dname):
                files = os.listdir(dname)
                matches = [f for f in files if f.startswith(rest)]
                start = cursor_pos - len(rest)

            else:
                return default

        else:
            start = cursor_pos - len(token)
            cmd = 'completion("%s")' % token
            output = self.scilab_wrapper._eval([cmd])
            if not output:
                return default

            matches = output.replace('!', ' ').split()
            for item in dir(self.scilab_wrapper):
                if item.startswith(token) and not item in matches:
                    matches.append(item)

        return {'matches': matches, 'cursor_start': start,
                'cursor_end': cursor_pos, 'metadata': dict(),
                'status': 'ok'}

    def do_inspect(self, code, cursor_pos, detail_level=0):
        """If the code ends with a (, try to return a calltip docstring"""
        default = {'status': 'aborted', 'data': dict(), 'metadata': dict()}
        # TODO: display for user-defined functions or variables
        return default

    def do_history(self, hist_access_type, output, raw, session=None,
                   start=None, stop=None, n=None, pattern=None, unique=False):
        """Access history at startup.
        """
        if not self.hist_file:
            return {'history': []}

        if not os.path.exists(self.hist_file):
            with open(self.hist_file, 'wb') as fid:
                fid.write('')

        with open(self.hist_file, 'rb') as fid:
            history = fid.readlines()

        history = history[:self.max_hist_cache]
        self.hist_cache = history
        self.log.debug('**HISTORY:')
        self.log.debug(history)
        history = [(None, None, h) for h in history]

        return {'history': history}

    def do_shutdown(self, restart):
        """Shut down the app gracefully, saving history.
        """
        self.log.debug("**Shutting down")

        if restart:
            self.scilab_wrapper.restart()

        else:
            self.scilab_wrapper.close()

        if self.hist_file:
            with open(self.hist_file, 'wb') as fid:
                fid.write('\n'.join(self.hist_cache[-self.max_hist_cache:]))

        return {'status': 'ok', 'restart': restart}

    def _get_help(self, code):
        code = code.replace('?', '')
        tokens = code.replace(';', ' ').split()
        if not tokens:
            return
        token = tokens[-1]

        if not self.scilab_wrapper.exists(token) == 0:
            self.scilab_wrapper.help(token)

            output = 'Calling Help Browser for `%s`' % token
            stream_content = {'name': 'stdout', 'data': output}
            self.send_response(self.iopub_socket, 'stream', stream_content)

    def _handle_output(self, output, silent):
        if output is None:
            output = ''
        else:
            self.scilab_wrapper.put('last_kernel_value', output)
            output = str(output)

        if not silent:
            stream_content = {'name': 'stdout', 'data': output}
            self.send_response(self.iopub_socket, 'stream', stream_content)

        return output

    def _handle_error(self, err):
        if 'parse error:' in err:
            err = 'Parse Error'

        elif 'Scilab returned:' in err:
            err = err[err.index('Scilab returned:'):]
            err = err[len('Scilab returned:'):].lstrip()

        elif 'Syntax Error' in err:
            err = 'Syntax Error'

        stream_content = {'name': 'stdout', 'data': err.strip()}
        self.send_response(self.iopub_socket, 'stream', stream_content)

        return {'status': 'error', 'execution_count': self.execution_count,
                'ename': '', 'evalue': err, 'traceback': []}

if __name__ == '__main__':
    from IPython.kernel.zmq.kernelapp import IPKernelApp
    IPKernelApp.launch_instance(kernel_class=ScilabKernel)
