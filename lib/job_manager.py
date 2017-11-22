import os
import shutil
import subprocess
import datetime
import threading
import re
from config_reader import AppConfigReader
from pony.orm import db_session, select
from database import db, Job
from lib.Fasta import Fasta
from lib.functions import Functions
import requests
import wget
from jinja2 import Template
import traceback
from pathlib import Path
from urllib import request, parse


class JobManager:

    def __init__(self, id_job: str, email: str=None, query: Fasta=None, target: Fasta=None, mailer=None):
        self.id_job = id_job
        self.email = email
        self.query = query
        self.target = target
        config_reader = AppConfigReader()
        self.error = ""
        # Get configs:
        self.batch_system_type = config_reader.get_batch_system_type()
        self.minimap2 = config_reader.get_minimap2_exec()
        self.threads = config_reader.get_nb_threads()
        self.app_data = config_reader.get_app_data()
        self.web_url = config_reader.get_web_url()
        self.mail_status = config_reader.get_mail_status_sender()
        self.mail_reply = config_reader.get_mail_reply()
        self.mail_org = config_reader.get_mail_org()
        self.do_send = config_reader.get_send_mail_status()
        # Outputs:
        self.output_dir = os.path.join(self.app_data, id_job)
        self.paf = os.path.join(self.output_dir, "map.paf")
        self.paf_raw = os.path.join(self.output_dir, "map_raw.paf")
        self.idx_q = os.path.join(self.output_dir, "query.idx")
        self.idx_t = os.path.join(self.output_dir, "target.idx")
        self.logs = os.path.join(self.output_dir, "logs.txt")
        self.mailer = mailer

    def set_inputs_from_res_dir(self):
        res_dir = os.path.join(self.app_data, self.id_job)
        query_file = os.path.join(res_dir, ".query")
        if os.path.exists(query_file):
            with open(query_file) as q_f:
                file_path = q_f.readline()
                self.query = Fasta(
                    name=os.path.splitext(os.path.basename(file_path.replace(".gz", "")).split("_", 1)[1])[0],
                    path=file_path,
                    type_f="local"
                )
        target_file = os.path.join(res_dir, ".target")
        if os.path.exists(target_file):
            with open(target_file) as t_f:
                file_path = t_f.readline()
                self.target = Fasta(
                    name=os.path.splitext(os.path.basename(file_path.replace(".gz", "")).split("_", 1)[1])[0],
                    path=file_path,
                    type_f="local"
                )

    def __check_job_success_local(self):
        if os.path.exists(self.paf):
            if os.path.getsize(self.paf) > 0:
                return "success"
            else:
                return "no-match"
        return "error"

    def check_job_success(self):
        if self.batch_system_type == "local":
            return self.__check_job_success_local()

    def get_mail_content(self, status):
        message = "D-Genies\n\n"
        if status == "success":
            message += "Your job %s was completed successfully!\n\n" % self.id_job
            message += str("Your job {0} is finished. You can see  the results by clicking on the link below:\n"
                           "{1}/result/{0}\n\n").format(self.id_job, self.web_url)
        else:
            message += "Your job %s has failed!\n\n" % self.id_job
            if self.error != "":
                message += self.error.replace("#ID#", self.id_job).replace("<br/>", "\n")
            else:
                message += "Your job %s has failed. You can try again. " \
                           "If the problem persists, please contact the support.\n\n" % self.id_job
        message += "Sequences compared in this analysis:\n"
        if self.query is not None:
            message += "Target: %s\nQuery: %s\n\n" % (self.target.get_name(), self.query.get_name())
        else:
            message += "Target: %s\n\n" % self.target.get_name()
        message += "See you soon on D-Genies,\n"
        message += "The team"
        return message

    def get_mail_content_html(self, status):
        with open(os.path.join(os.path.dirname(os.path.realpath(__file__)), "mail_templates", "job_notification.html"))\
                as t_file:
            template = Template(t_file.read())
            return template.render(job_name=self.id_job, status=status, url_base=self.web_url,
                                   query_name=self.query.get_name() if self.query is not None else "",
                                   target_name=self.target.get_name(),
                                   error=self.error)

    def get_mail_subject(self, status):
        if status == "success" or status == "no-match":
            return "DGenies - Job completed: %s" % self.id_job
        else:
            return "DGenies - Job failed: %s" % self.id_job

    @db_session
    def send_mail(self):
        # Retrieve infos:
        job = Job.get(id_job=self.id_job)
        if self.email is None:
            self.email = job.email
        status = job.status
        self.error = job.error

        # Send:
        self.mailer.send_mail([self.email], self.get_mail_subject(status), self.get_mail_content(status),
                              self.get_mail_content_html(status))

    def search_error(self):
        logs = os.path.join(self.output_dir, "logs.txt")
        if os.path.exists(logs):
            lines = subprocess.check_output(['tail', '-2', logs]).decode("utf-8").split("\n")
            if re.match(r"\[morecore] \d+ bytes requested but not available.", lines[1]) or \
                    re.match(r"\[morecore] \d+ bytes requested but not available.", lines[1]) or \
                    re.match(r"\[morecore] insufficient memory", lines[0]) or \
                    re.match(r"\[morecore] insufficient memory", lines[1]):
                return "Your job #ID# has failed because of memory limit exceeded. May be your sequences are too big?" \
                       "<br/>You can contact the support for more information."
        return "Your job #ID# has failed. You can try again.<br/>If the problem persists, please contact the support."

    @db_session
    def __launch_local(self):
        cmd = ["run_minimap2.sh", self.minimap2, self.threads, self.target.get_path(),
               self.query.get_path() if self.query is not None else "NONE", self.paf, self.paf_raw]
        with open(self.logs, "w") as logs:
            p = subprocess.Popen(cmd, stdout=logs, stderr=logs)
        job = Job.get(id_job=self.id_job)
        job.id_process = p.pid
        job.status = "started"
        db.commit()
        p.wait()
        if p.returncode == 0:
            status = self.check_job_success()
            job.status = status
            db.commit()
            return status == "success"
        job.status = "error"
        self.error = self.search_error()
        job.error = self.error
        db.commit()
        return False

    def __getting_local_file(self, fasta: Fasta, type_f):
        finale_path = os.path.join(self.output_dir, type_f + "_" + os.path.basename(fasta.get_path()))
        shutil.move(fasta.get_path(), finale_path)
        with open(os.path.join(self.output_dir, "." + type_f), "w") as save_file:
            save_file.write(finale_path)
        return finale_path

    def __getting_file_from_url(self, fasta: Fasta, type_f):
        dl_path = wget.download(fasta.get_path(), self.output_dir, None)
        filename = os.path.basename(dl_path)
        name = os.path.splitext(filename.replace(".gz", ""))[0]
        finale_path = os.path.join(self.output_dir, type_f + "_" + filename)
        shutil.move(dl_path, finale_path)
        with open(os.path.join(self.output_dir, "." + type_f), "w") as save_file:
            save_file.write(finale_path)
        return finale_path, name

    @db_session
    def __check_url(self, fasta: Fasta):
        url = fasta.get_path()
        if url.startswith("http://") or url.startswith("https://"):
            filename = requests.head(url, allow_redirects=True).url.split("/")[-1]
        elif url.startswith("ftp://"):
            filename = url.split("/")[-1]
        else:
            filename = None
        if filename is not None:
            allowed = Functions.allowed_file(filename)
            if not allowed:
                job = Job.get(id_job=self.id_job)
                job.status = "error"
                job.error = "<p>File <b>%s</b> downloaded from <b>%s</b> is not a Fasta file!</p>" \
                            "<p>If this is unattended, please contact the support.</p>" % (filename, url)
                db.commit()
        else:
            allowed = False
            job = Job.get(id_job=self.id_job)
            job.status = "error"
            job.error = "<p>Url <b>%s</b> is not a valid URL!</p>" \
                        "<p>If this is unattended, please contact the support.</p>" % (url)
            db.commit()
        return allowed

    @db_session
    def getting_files(self):
        job = Job.get(id_job=self.id_job)
        job.status = "getfiles"
        db.commit()
        correct = True
        if self.query is not None:
            if self.query.get_type() == "local":
                self.query.set_path(self.__getting_local_file(self.query, "query"))
            elif self.__check_url(self.query):
                finale_path, filename = self.__getting_file_from_url(self.query, "query")
                self.query.set_path(finale_path)
                self.query.set_name(filename)
            else:
                correct = False
        if correct and self.target is not None:
            if self.target.get_type() == "local":
                self.target.set_path(self.__getting_local_file(self.target, "target"))
            elif self.__check_url(self.target):
                finale_path, filename = self.__getting_file_from_url(self.target, "target")
                self.target.set_path(finale_path)
                self.target.set_name(filename)
            else:
                correct = False
        return correct

    def send_mail_post(self):
        """
        Send mail using POST url (we have no access to mailer)
        """
        key = Functions.random_string(15)
        key_file = os.path.join(self.app_data, self.id_job, ".key")
        with open(key_file, "w") as k_f:
            k_f.write(key)
        data = parse.urlencode({"key": key}).encode()
        req = request.Request(self.web_url + "/send-mail/" + self.id_job, data=data)
        resp = request.urlopen(req)
        if resp.getcode() != 200:
            print("Job %s: Send mail failed!" % self.id_job)

    def run_job_in_thread(self):
        thread = threading.Timer(1, self.run_job, kwargs={"batch_system_type": "local"})
        thread.start()  # Start the execution

    @db_session
    def run_job(self, batch_system_type):
        success = False
        if batch_system_type == "local":
            success = self.__launch_local()
        if success:
            job = Job.get(id_job=self.id_job)
            job.status = "indexing"
            db.commit()
            target_index = os.path.join(self.output_dir, "target.idx")
            Functions.index_file(self.target, target_index)
            query_index = os.path.join(self.output_dir, "query.idx")
            if self.query is not None:
                Functions.index_file(self.query, query_index)
            else:
                shutil.copyfile(target_index, query_index)
                Path(os.path.join(self.output_dir, ".all-vs-all")).touch()
            job = Job.get(id_job=self.id_job)
            job.status = "success"
            db.commit()
        if self.do_send:
            self.send_mail_post()
            #self.send_mail(job.status)

    @db_session
    def start_job(self):
        try:
            success = self.getting_files()
            if success:
                job = Job.get(id_job=self.id_job)
                job.status = "waiting"
                db.commit()
            else:
                job = Job.get(id_job=self.id_job)
                job.status = "error"
                job.error = "<p>Error while getting input files. Please contact the support to report the bug.</p>"
                db.commit()
                if self.do_send:
                    self.send_mail()

        except Exception:
            print(traceback.print_exc())
            job = Job.get(id_job=self.id_job)
            job.status = "error"
            job.error = "<p>An unexpected error has occurred. Please contact the support to report the bug.</p>"
            db.commit()
            if self.do_send:
                self.send_mail()


    @db_session
    def launch(self):
        j1 = select(j for j in Job if j.id_job == self.id_job)
        if len(j1) > 0:
            print("Old job found without result dir existing: delete it from BDD!")
            j1.delete()
        if self.target is not None:
            job = Job(id_job=self.id_job, email=self.email, batch_type=self.batch_system_type,
                      date_created=datetime.datetime.now())
            db.commit()
            if not os.path.exists(self.output_dir):
                os.mkdir(self.output_dir)
            thread = threading.Timer(1, self.start_job)
            thread.start()
        else:
            job = Job(id_job=self.id_job, email=self.email, batch_type=self.batch_system_type,
                      date_created=datetime.datetime.now(), status="error")
            db.commit()

    @db_session
    def status(self):
        job = Job.get(id_job=self.id_job)
        if job is not None:
            return job.status, job.error
        else:
            return "unknown", ""
