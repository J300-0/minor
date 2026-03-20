%% elsarticle.cls — Minimal shim for Elsevier article format.
%% Provides \journal, \fnref, \fntext, \cortext, \address, frontmatter,
%% keyword env used by the Elsevier template.
%% If you have the official elsarticle.cls from Elsevier, replace this file.
%%
\NeedsTeXFormat{LaTeX2e}
\ProvidesClass{elsarticle}[2024/01/01 Minimal Elsevier shim]

\DeclareOption{preprint}{}
\DeclareOption{review}{}
\DeclareOption{authoryear}{}
\DeclareOption{number}{}
\DeclareOption{sort&compress}{}
\DeclareOption*{\PassOptionsToClass{\CurrentOption}{article}}
\ProcessOptions\relax
\LoadClass{article}

\usepackage[margin=1in]{geometry}

% Journal name
\newcommand{\journal}[1]{}

% Elsevier-specific author commands
\newcommand{\address}[2][]{\par{\small\itshape #2\par}}
\newcommand{\fnref}[1]{\textsuperscript{#1}}
\newcommand{\fntext}[2][]{\footnotetext[0]{#2}}
\newcommand{\cortext}[2][]{}
\newcommand{\corref}[1]{}
\newcommand{\ead}[2][]{\texttt{#2}}

% Keyword environment
\newenvironment{keyword}{%
  \par\smallskip\noindent\textbf{Keywords:}\space
}{%
  \par\bigskip
}
\newcommand{\sep}{;\space}
\newcommand{\MSC}[1]{}
\newcommand{\PACS}[1]{}
\newcommand{\JEL}[1]{}

% frontmatter environment
\newenvironment{frontmatter}{\begingroup}{\endgroup\maketitle}
