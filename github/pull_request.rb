require 'octokit'


class PullRequest

  attr_accessor :raw_data, :title, :issue_numbers, :repo, :number, :client, :commits

  def initialize(raw_data)
    self.raw_data = raw_data
    self.title    = raw_data['title']
    self.repo     = raw_data['base']['repo']['full_name']
    self.number   = raw_data['number']
    self.client   = Octokit::Client.new(:access_token => ENV['GITHUB_OAUTH_TOKEN'])
    self.commits  = client.pull_commits(repo, number)

    self.issue_numbers = []
    title.scan(/([\s\(\[,-]|^)(fixes|refs)[\s:]+(#\d+([\s,;&]+#\d+)*)(?=[[:punct:]]|\s|<|$)/i) do |match|
      action, refs = match[1].to_s.downcase, match[2]
      next if action.empty?
      refs.scan(/#(\d+)/).each { |m| self.issue_numbers << m[0].to_i }
    end
  end

  def new?
    @raw_data['created_at'] == @raw_data['updated_at']
  end

  def set_labels
    labels = ["Needs testing", "Not yet reviewed"]
    @client.add_labels_to_an_issue(@repo, @number, labels)
  end

  def check_commits_style
    warnings = ''
    @commits.each do |commit|
      if (commit.commit.message.lines.first =~ /\A(fixes|refs) #\d+(, ?#\d+)*(:| -) .*\Z/i) != 0
        warnings += "  * #{commit.sha} must be in the format ```Fixes/refs #redmine_number - brief description```.\n"
      end
    end
    message = <<EOM
There were the following issues with the commit message:
#{warnings}

Guidelines are available on [the Foreman wiki](http://projects.theforeman.org/projects/foreman/wiki/Reviewing_patches-commit_message_format).

---------------------------------------
This message was auto-generated by Foreman's [prprocessor](http://github.com/theforeman/prprocessor)
EOM
    add_comment(message) unless warnings.empty?
  end

  private

  def add_comment(message)
    @client.add_comment(@repo, @number, message)
  end

end
