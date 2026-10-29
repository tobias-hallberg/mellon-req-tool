%% syrs.cls — System Requirements Specification class
%% Modernized formal aesthetic, A4, automotive flavor (ISO 26262)
%% Compile with XeLaTeX or LuaLaTeX (for Inter / IBM Plex Mono)
%% Falls back to lmodern if those fonts aren't installed.

\NeedsTeXFormat{LaTeX2e}
\ProvidesClass{syrs}[2026/04/27 System Requirements Specification class]

\LoadClass[11pt,a4paper,oneside]{article}

%% ---- Engine detection ---------------------------------------------------
\RequirePackage{iftex}
\ifPDFTeX
  \RequirePackage[T1]{fontenc}
  \RequirePackage[utf8]{inputenc}
  \RequirePackage{lmodern}
  \newcommand{\syrsmono}[1]{{\ttfamily #1}}
\else
  \RequirePackage{fontspec}
  %% Use Inter + IBM Plex Mono if installed; lmodern otherwise.
  \IfFontExistsTF{Inter}{
    \setmainfont{Inter}[
      UprightFont = * Regular,
      BoldFont    = * SemiBold,
      ItalicFont  = * Italic,
      Numbers     = {Lining,Tabular},
    ]
    \setsansfont{Inter}[
      UprightFont = * Regular,
      BoldFont    = * SemiBold,
      ItalicFont  = * Italic,
    ]
  }{
    \RequirePackage{lmodern}
  }
  \IfFontExistsTF{IBM Plex Mono}{
    \setmonofont{IBM Plex Mono}[Scale=0.92]
  }{}
  \newcommand{\syrsmono}[1]{{\ttfamily #1}}
\fi

%% ---- Packages -----------------------------------------------------------
\RequirePackage[a4paper,
  top=20mm, bottom=18mm, left=26mm, right=18mm,
  headheight=10mm, headsep=4mm, footskip=10mm]{geometry}
\RequirePackage{xcolor}
\RequirePackage{fancyhdr}
\RequirePackage{titlesec}
\RequirePackage{titletoc}
\RequirePackage{enumitem}
\RequirePackage{array}
\RequirePackage{tabularx}
\RequirePackage{longtable}
\RequirePackage{booktabs}
\RequirePackage{multirow}
\RequirePackage{colortbl}
\RequirePackage{tikz}
\RequirePackage{graphicx}  %% already loaded by tikz; figures rely on it
%% pdftitle / pdfauthor come from \@docSubject / \@docOwner (see below).
\RequirePackage[hidelinks,bookmarks=true]{hyperref}
\RequirePackage{microtype}
%% microtype's \textls letterspacing isn't supported under XeLaTeX (which
%% tectonic uses by default). Fall back to plain text so the cls keeps
%% working — the visual difference is minor and limited to a few
%% small-caps eyebrows.
\ifXeTeX
  \renewcommand{\textls}[2][]{#2}
\fi
\RequirePackage{calc}
\RequirePackage{etoolbox}
%% \RequirePackage{fontawesome5}  %% optional; mellon template doesn't use it
\RequirePackage{ragged2e}

\usetikzlibrary{positioning,calc,shapes.geometric,arrows.meta}

%% ---- Color palette (paper & ink) ---------------------------------------
\definecolor{paper}{HTML}{FFFFFF}          %% plain white: prints cleanly, and figures (white-backed) sit flush
\definecolor{paperEdge}{HTML}{ECEAE2}
\definecolor{ink}{HTML}{15171A}
\definecolor{inkTwo}{HTML}{3A3D42}
\definecolor{inkThree}{HTML}{6A6E75}
\definecolor{inkFour}{HTML}{A0A4AB}
\definecolor{rule}{HTML}{C9C7BE}
\definecolor{ruleTwo}{HTML}{E2E0D7}
\definecolor{ruleThree}{HTML}{EFEDE5}
\definecolor{accent}{HTML}{2E7F8A}        %% teal — matches design oklch(55% 0.08 195)
\definecolor{accentSoft}{HTML}{E6F0F1}
\definecolor{accentLine}{HTML}{8FB6BB}
\definecolor{statusDraft}{HTML}{8C6A2E}
\definecolor{statusApproved}{HTML}{2E6A4A}
\definecolor{statusImplemented}{HTML}{45567E}
\definecolor{priMust}{HTML}{8C3A22}
\definecolor{priShould}{HTML}{8C6A2E}
\definecolor{priCould}{HTML}{6A7280}
\definecolor{sectionRowBg}{HTML}{F1EFE6}

\pagecolor{paper}

%% ---- Document metadata variables ---------------------------------------
%% All empty by default: a field nobody set is left off the cover, header
%% and footer instead of printing a placeholder.
\newcommand{\@docID}{}
\newcommand{\@docRev}{}
\newcommand{\@docDate}{}
\newcommand{\@docClass}{}
\newcommand{\@docSubject}{}
\newcommand{\@docProject}{}
\newcommand{\@docOwner}{}
\newcommand{\@docASIL}{}
\newcommand{\@docStandard}{}
\newcommand{\@docType}{}

\newcommand{\setDocID}[1]{\renewcommand{\@docID}{#1}}
\newcommand{\setDocRev}[1]{\renewcommand{\@docRev}{#1}}
\newcommand{\setDocDate}[1]{\renewcommand{\@docDate}{#1}}
\newcommand{\setDocClassification}[1]{\renewcommand{\@docClass}{#1}}
\newcommand{\setDocSubject}[1]{\renewcommand{\@docSubject}{#1}}
\newcommand{\setDocProject}[1]{\renewcommand{\@docProject}{#1}}
\newcommand{\setDocOwner}[1]{\renewcommand{\@docOwner}{#1}}
\newcommand{\setDocASIL}[1]{\renewcommand{\@docASIL}{#1}}
\newcommand{\setDocStandard}[1]{\renewcommand{\@docStandard}{#1}}
%% Document-type eyebrow above the cover title (e.g. "System Requirements
%% Specification"); hidden when unset.
\newcommand{\setDocType}[1]{\renewcommand{\@docType}{#1}}

\AtBeginDocument{\hypersetup{pdftitle={\@docSubject},pdfauthor={\@docOwner}}}

%% "Rev. A.00 • 2026-01-01", leaving out whichever half is unset.
\newcommand{\syrs@revdate}{%
  \ifdefempty{\@docRev}{\@docDate}{%
    Rev.\ \@docRev\ifdefempty{\@docDate}{}{\ \textbullet\ \@docDate}}}
%% "DOC-ID • Rev. A.00", or just the id when no revision is set.
\newcommand{\syrs@idrev}{%
  \@docID\ifdefempty{\@docRev}{}{\ \textbullet\ Rev.\ \@docRev}}

%% ---- Running header / footer -------------------------------------------
\pagestyle{fancy}
\fancyhf{}
\renewcommand{\headrulewidth}{0pt}
\renewcommand{\footrulewidth}{0pt}

%% Header: value above label on each side (matches design screenshots).
%% \leavevmode keeps `\\` legal when the first line is empty.
\fancyhead[L]{%
  \sffamily\leavevmode%
  {\color{inkTwo}\syrsmono{\fontsize{8pt}{10pt}\selectfont\@docID}}\\[-1pt]%
  {\color{inkFour}\fontsize{6.5pt}{8pt}\selectfont%
   \MakeUppercase{\textls[60]{\@docSubject}}}%
}
\fancyhead[R]{%
  \sffamily\raggedleft\leavevmode%
  {\color{inkTwo}\fontsize{8pt}{10pt}\selectfont%
   \syrs@revdate}\\[-1pt]%
  {\color{inkFour}\fontsize{6.5pt}{8pt}\selectfont%
   \MakeUppercase{\textls[60]{\@docClass}}}%
}
\fancyfoot[L]{\sffamily\color{inkThree}\fontsize{7.5pt}{9pt}\selectfont%
  \syrsmono{\syrs@idrev}}
\fancyfoot[C]{\sffamily\color{inkTwo}\fontsize{7.5pt}{9pt}\selectfont%
  \MakeUppercase{\textls[120]{\@docClass}}}
\fancyfoot[R]{\sffamily\color{inkThree}\fontsize{7.5pt}{9pt}\selectfont%
  Page \thepage\ of \pageref*{LastPage}}

\RequirePackage{lastpage}

%% Header bottom rule
\renewcommand{\headrule}{\vspace{1mm}\color{ruleTwo}\hrule height 0.4pt}

%% ---- Heading styles ----------------------------------------------------
%% Bypass titlesec for section headings — the template calls our custom
%% \syrssection / \syrssubsection / \syrssubsubsection commands directly.
%% This avoids \tableofcontents' internal \section*{\contentsname} call
%% inheriting the "SECTION N" eyebrow.

\newcommand{\syrssection}[1]{%
  \par\addvspace{7mm}%
  \refstepcounter{section}%
  {\sffamily\bfseries\color{ink}\fontsize{20pt}{24pt}\selectfont
   \raggedright \thesection.\hspace{0.4em}#1\par}%
  \addcontentsline{toc}{section}{\protect\numberline{\thesection}#1}%
  \vspace{2.5mm}%
}

\newcommand{\syrssubsection}[1]{%
  \par\addvspace{4mm}%
  \refstepcounter{subsection}%
  \noindent
  \begin{minipage}[t]{14mm}\raggedright
    {\sffamily\color{inkThree}\fontsize{9pt}{11pt}\selectfont%
     \syrsmono{\thesubsection}}%
  \end{minipage}%
  \begin{minipage}[t]{\dimexpr\linewidth-14mm}%
    {\sffamily\bfseries\color{ink}\fontsize{13pt}{16pt}\selectfont
     \raggedright #1}%
  \end{minipage}\par
  \addcontentsline{toc}{subsection}{\protect\numberline{\thesubsection}#1}%
  \vspace{1.5mm}%
}

\newcommand{\syrssubsubsection}[1]{%
  \par\addvspace{3mm}%
  \refstepcounter{subsubsection}%
  \noindent
  \begin{minipage}[t]{14mm}\raggedright
    {\sffamily\color{inkThree}\fontsize{8.5pt}{10pt}\selectfont%
     \syrsmono{\thesubsubsection}}%
  \end{minipage}%
  \begin{minipage}[t]{\dimexpr\linewidth-14mm}%
    {\sffamily\bfseries\color{ink}\fontsize{11pt}{13pt}\selectfont
     \raggedright #1}%
  \end{minipage}\par
  \addcontentsline{toc}{subsubsection}{\protect\numberline{\thesubsubsection}#1}%
  \vspace{1.2mm}%
}

\newcommand{\syrsparagraph}[1]{%
  \par\addvspace{2.5mm}%
  {\sffamily\bfseries\color{ink}\fontsize{10pt}{12pt}\selectfont #1\par}%
  \vspace{1mm}%
}

%% Section eyebrow ("Section N")
\newcommand{\sectioneyebrow}[1]{%
  {\sffamily\color{inkThree}\fontsize{7.5pt}{9pt}\selectfont%
   \MakeUppercase{\textls[60]{#1}}}\par\vspace{1mm}}

%% Document-level H1
\newcommand{\doctitle}[1]{%
  {\sffamily\color{ink}\fontsize{20pt}{22pt}\selectfont\bfseries #1\par}\vspace{4mm}}

%% Lead paragraph
\newcommand{\lead}[1]{%
  {\color{inkTwo}\fontsize{10.5pt}{14pt}\selectfont #1\par}\vspace{3mm}}

%% ---- Cover page --------------------------------------------------------
%% Layout:
%%   - top: centered "CONTROLLED DOCUMENT --- INTERNAL USE ONLY" between two
%%     thin black rules
%%   - org line: small filled-square mark + uppercase mono \@docProject
%%   - doctype eyebrow (teal mono) \@docType
%%   - big SemiBold left-aligned title: \@docSubject
%%   - thin rule, then 4-column metadata grid
%%   - footer: prepared-by left, page count + doc id right
%% Any of these whose field is unset (classification, project, doctype,
%% standard, a grid cell, the owner) is left out.

\newcommand{\covermark}{%
  \tikz[baseline=-0.7ex]\fill[ink] (0,0) rectangle (3.2mm,3.2mm);}

\newcommand{\covercell}[2]{%
  {\sffamily\fontsize{7pt}{8pt}\selectfont\color{inkFour}%
   \MakeUppercase{\textls[80]{#1}}\par}\vspace{0.8mm}%
  {\sffamily\color{ink}\fontsize{9pt}{11pt}\selectfont #2}%
}

%% Metadata grid body: only the cells that have a value, four per row.
%% \syrs@gridcell{label}{\@field}{display}
\newcounter{syrs@col}
\newcommand{\syrs@grid}{}
\newcommand{\syrs@gridcell}[3]{%
  \ifdefempty{#2}{}{%
    \ifnumequal{\value{syrs@col}}{4}{%
      \gappto\syrs@grid{\\[5mm]}\setcounter{syrs@col}{0}}{}%
    \ifnumequal{\value{syrs@col}}{0}{}{\gappto\syrs@grid{&}}%
    \gappto\syrs@grid{\covercell{#1}{#3}}%
    \stepcounter{syrs@col}}}
%% Pad the last row to four cells: TeX drops a column no row reaches, and
%% tabularx would then share the width among fewer, wider columns.
\newcommand{\syrs@padgrid}{%
  \ifnumless{\value{syrs@col}}{4}{%
    \gappto\syrs@grid{&}\stepcounter{syrs@col}\syrs@padgrid}{}}

\newcommand{\makecoverpage}{%
  \begin{titlepage}%
    \pagecolor{paper}%
    \thispagestyle{empty}%
    \noindent
    %% Top classification banner: rules above and below, centered text.
    \vspace*{14mm}
    {\color{ink}\hrule height 0.5pt}
    \vspace{1.6mm}
    \ifdefempty{\@docClass}{}{%
      \centerline{\sffamily\fontsize{8pt}{10pt}\selectfont\color{ink}%
        \syrsmono{\MakeUppercase{\textls[200]{Controlled Document\,\char"2014\,\@docClass}}}}%
      \vspace{1.6mm}}
    {\color{ink}\hrule height 0.5pt}

    \vspace{28mm}

    %% Org line: small black square mark + project name (uppercase mono)
    \ifdefempty{\@docProject}{}{%
      \noindent\covermark\hspace{3mm}%
      {\sffamily\color{inkTwo}\fontsize{8.5pt}{10pt}\selectfont%
        \syrsmono{\MakeUppercase{\textls[150]{\@docProject}}}\par}}

    \vspace{18mm}

    %% Doctype eyebrow
    \ifdefempty{\@docType}{}{%
      {\sffamily\color{accent}\fontsize{9pt}{11pt}\selectfont%
        \syrsmono{\MakeUppercase{\textls[180]{\@docType}}}\par}%
      \vspace{4mm}}

    %% Title — the document subject, big SemiBold sans
    \begingroup
      \sffamily\bfseries\color{ink}%
      \fontsize{34pt}{38pt}\selectfont%
      \setlength{\parskip}{0pt}%
      \raggedright
      \@docSubject\par
    \endgroup
    \vspace{6mm}

    %% Subtitle (issue / standard line)
    \ifdefempty{\@docStandard}{}{%
      {\sffamily\color{inkTwo}\fontsize{11pt}{15pt}\selectfont%
        \@docStandard\par}}

    \vspace{14mm}
    {\color{rule}\hrule height 0.4pt}
    \vspace{8mm}

    %% Metadata grid — up to two rows of four cells.
    \gdef\syrs@grid{}\setcounter{syrs@col}{0}%
    \syrs@gridcell{Document ID}{\@docID}{\syrsmono{\@docID}}%
    \syrs@gridcell{Revision}{\@docRev}{\@docRev}%
    \syrs@gridcell{Issue Date}{\@docDate}{\@docDate}%
    \syrs@gridcell{Owner}{\@docOwner}{\@docOwner}%
    \syrs@gridcell{Standard}{\@docStandard}{\@docStandard}%
    \syrs@gridcell{ASIL}{\@docASIL}{\@docASIL}%
    \syrs@gridcell{Classification}{\@docClass}{\@docClass}%
    \ifnumequal{\value{syrs@col}}{0}{\setcounter{syrs@col}{1}}{}%
    \syrs@padgrid
    \noindent
    \renewcommand{\arraystretch}{1.0}
    \setlength{\tabcolsep}{0pt}%
    \begin{tabularx}{\linewidth}{@{}*{4}{>{\RaggedRight\arraybackslash}X}@{}}
      \syrs@grid
    \end{tabularx}

    \vfill

    %% Footer band
    \noindent
    \begin{tabularx}{\linewidth}{@{}X@{}>{\raggedleft\arraybackslash}X@{}}
      \ifdefempty{\@docOwner}{}{%
        {\sffamily\fontsize{7pt}{9pt}\selectfont\color{inkFour}%
         \MakeUppercase{\textls[80]{Prepared By}}}\newline
        {\sffamily\color{inkTwo}\fontsize{8.5pt}{11pt}\selectfont\@docOwner}}
      &
      {\sffamily\color{inkThree}\fontsize{7.5pt}{10pt}\selectfont%
        \syrsmono{Page 1 of \pageref*{LastPage}}}\newline
      {\sffamily\color{inkTwo}\fontsize{8.5pt}{11pt}\selectfont%
        \syrsmono{\syrs@idrev}}
    \end{tabularx}
  \end{titlepage}%
  \pagecolor{paper}%
}

%% ---- Document control table ------------------------------------------
\newenvironment{docattrs}{%
  \renewcommand{\arraystretch}{1.3}%
  \noindent\begin{tabularx}{\linewidth}{@{}>{\sffamily\color{inkThree}\fontsize{8pt}{10pt}\selectfont}p{38mm}>{\sffamily\color{ink}}X@{}}
}{%
  \end{tabularx}\par%
}
\newcommand{\docrow}[2]{#1 & #2 \\[0.6mm] \arrayrulecolor{ruleTwo}\hline\noalign{\vskip 0.6mm}}

%% Revision history
\newenvironment{revhistory}{%
  \renewcommand{\arraystretch}{1.25}%
  \arrayrulecolor{ruleTwo}%
  \noindent\begin{tabularx}{\linewidth}{@{}p{16mm}p{22mm}p{32mm}X@{}}
  \toprule
  \rowcolor{paper}
  {\sffamily\color{inkThree}\fontsize{7pt}{9pt}\selectfont\MakeUppercase{Revision}} &
  {\sffamily\color{inkThree}\fontsize{7pt}{9pt}\selectfont\MakeUppercase{Date}} &
  {\sffamily\color{inkThree}\fontsize{7pt}{9pt}\selectfont\MakeUppercase{Author}} &
  {\sffamily\color{inkThree}\fontsize{7pt}{9pt}\selectfont\MakeUppercase{Summary of Change}} \\
  \midrule
}{%
  \bottomrule\end{tabularx}\par%
}
\newcommand{\revrow}[4]{\syrsmono{#1} & \syrsmono{#2} & #3 & #4 \\}

%% ---- Requirement environment ------------------------------------------
%% \begin{requirement}{ID}{Title}
%%   \shall{...}
%%   \rationale{...}
%%   \attrs{Priority=Must, Status=Approved, Verification=Test, ASIL=B, Owner=ADAS-Sys}
%%   \trace{Parent}{STR-ADAS-021, SG-ACC-02}
%%   \trace{Child}{SWR-ACC-103, SWR-ACC-104}
%% \end{requirement}

%% Filled circle accent (matches design's accent dot before ID).
\newcommand{\reqaccent}{%
  \tikz[baseline=-0.4ex]\fill[accent] (0,0) circle (0.85mm);\hspace{1.6mm}}

%% Wrap each card in a minipage so it's atomic — no page break splits the
%% top rule from its content.
\newenvironment{requirement}[2]{%
  \par\noindent
  \begin{minipage}{\linewidth}%
  {\color{inkTwo}\rule{\linewidth}{1.2pt}}\par
  \vspace{0.6mm}
  \noindent{\sffamily\color{ink}\bfseries\reqaccent\syrsmono{\fontsize{9pt}{11pt}\selectfont #1}}%
  \ifstrempty{#2}{}{\quad{\sffamily\color{ink}\bfseries\fontsize{11pt}{13pt}\selectfont #2}}%
  \par
  \vspace{0.6mm}
}{%
  \par\vspace{0.4mm}
  {\color{rule}\rule{\linewidth}{0.4pt}}\par
  \end{minipage}\par
  \vspace{1.2mm}
}

%% [t]-minipages align by first-line baseline. The label is set in 7/9pt and
%% the description in 9.5/13pt, so without compensation the smaller label
%% sits visibly higher than the description text on a single-line card. A
%% \vphantom at the description's font metrics inflates the label line's
%% height to match, so both first lines share a top edge.
\newcommand{\shall}[1]{%
  \noindent
  \begin{minipage}[t]{20mm}%
    \vspace{0pt}%
    {\sffamily\color{accent}\fontsize{7pt}{9pt}\selectfont%
     \vphantom{\fontsize{9.5pt}{13pt}\selectfont X}%
     \MakeUppercase{\textls[80]{Requirement}}}%
  \end{minipage}%
  \begin{minipage}[t]{\dimexpr\linewidth-21mm}%
    \vspace{0pt}%
    \fontsize{9.5pt}{13pt}\selectfont #1%
  \end{minipage}\par\vspace{0.7mm}}

\newcommand{\rationale}[1]{%
  \noindent
  \begin{minipage}[t]{18mm}%
    {\sffamily\color{inkThree}\fontsize{7pt}{9pt}\selectfont\MakeUppercase{Rationale}}%
  \end{minipage}%
  \begin{minipage}[t]{\dimexpr\linewidth-19mm}%
    {\color{inkTwo}\fontsize{9pt}{12pt}\selectfont #1}%
  \end{minipage}\par\vspace{1.6mm}}

%% Marker line where an attached figure couldn't be rendered (draw.io not
%% installed, or the figure is empty) — says so instead of dropping it.
\newcommand{\figureplaceholder}[1]{%
  \par\noindent
  {\sffamily\color{inkThree}\fontsize{8pt}{10pt}\selectfont #1}\par
  \vspace{0.6mm}}

%% \syrsfigure{file}{caption} — a rendered figure, centred, then a numbered
%% caption line ("Figure N: caption", or just "Figure N" when the caption is
%% empty). Scaled down, never up, to the line width and to 0.45\textheight so
%% it still fits on one page inside an unbreakable requirement card.
\newsavebox{\syrs@figbox}
\newcommand{\syrsfigure}[2]{%
  \par\vspace{1.2mm}%
  \sbox\syrs@figbox{\includegraphics{#1}}%
  \ifdim\wd\syrs@figbox>\linewidth
    \sbox\syrs@figbox{\resizebox{\linewidth}{!}{\usebox\syrs@figbox}}%
  \fi
  \ifdim\ht\syrs@figbox>0.45\textheight
    \sbox\syrs@figbox{\resizebox{!}{0.45\textheight}{\usebox\syrs@figbox}}%
  \fi
  {\centering\usebox\syrs@figbox\par}%
  \refstepcounter{figure}%
  \vspace{1.2mm}%
  {\centering\sffamily\color{ink}\fontsize{8.5pt}{11pt}\selectfont
   \textbf{Figure~\thefigure}\ifstrempty{#2}{}{: #2}\par}%
  \vspace{1.2mm}}

%% \attrs renders a 5-column row of attribute cells. The argument is the
%% pre-formatted body of a tabularx row — emit a series of `\attrcell{K}{V}`
%% calls separated by `&`. The mellon exporter does that splitting in
%% Python; the original cls used an expl3-based parser, which broke under
%% tectonic's bundled expl3 version.
\newcommand{\attrcell}[2]{%
  \begin{minipage}[t]{\dimexpr\linewidth-3mm}%
    {\sffamily\color{inkFour}\fontsize{6.5pt}{8pt}\selectfont%
     \MakeUppercase{\textls[80]{#1}}}\\[0.4mm]%
    {\sffamily\color{ink}\fontsize{8.5pt}{11pt}\selectfont #2}%
  \end{minipage}%
}
%% Attribute strip with thin grey verticals between cells (matches design).
%% \arrayrulecolor + \setlength{\arrayrulewidth} colors the | separators.
\newcommand{\attrs}[1]{%
  \par\vspace{0.4mm}%
  {\color{ruleTwo}\rule{\linewidth}{0.4pt}}\par
  \vspace{0.4mm}%
  \noindent
  \setlength{\tabcolsep}{2mm}%
  \begin{tabularx}{\linewidth}{@{}%
    >{\RaggedRight\arraybackslash}X%
    !{\color{ruleTwo}\vrule width 0.4pt}%
    >{\RaggedRight\arraybackslash}X%
    !{\color{ruleTwo}\vrule width 0.4pt}%
    >{\RaggedRight\arraybackslash}X%
    !{\color{ruleTwo}\vrule width 0.4pt}%
    >{\RaggedRight\arraybackslash}X%
    !{\color{ruleTwo}\vrule width 0.4pt}%
    >{\RaggedRight\arraybackslash}X@{}}
    \rule{0pt}{0.4mm}#1\rule[-0.6mm]{0pt}{0.6mm}
  \end{tabularx}\par\vspace{0.6mm}%
}

%% ---- Status / priority pills -----------------------------------------
%% Map a value to a pill color via etoolbox's \ifstrequal chain. Anything
%% unrecognized falls through to a neutral grey pill.
\newcommand{\statuspill}[1]{%
  \ifstrequal{#1}{Approved}{\tag{statusApproved}{#1}}{%
  \ifstrequal{#1}{Reviewed}{\tag{statusApproved}{#1}}{%
  \ifstrequal{#1}{Implemented}{\tag{statusImplemented}{#1}}{%
  \ifstrequal{#1}{Draft}{\tag{statusDraft}{#1}}{%
  \tag{inkThree}{#1}}}}}%
}
\newcommand{\prioritypill}[1]{%
  \ifstrequal{#1}{Must}{\tag{priMust}{#1}}{%
  \ifstrequal{#1}{Should}{\tag{priShould}{#1}}{%
  \ifstrequal{#1}{Could}{\tag{priCould}{#1}}{%
  \tag{inkThree}{#1}}}}%
}

%% Trace row
\newcommand{\trace}[2]{%
  \noindent
  \begin{minipage}[t]{18mm}%
    {\sffamily\color{inkThree}\fontsize{7pt}{9pt}\selectfont\MakeUppercase{#1}}%
  \end{minipage}%
  \begin{minipage}[t]{\dimexpr\linewidth-19mm}%
    {\syrsmono\fontsize{7.5pt}{10pt}\selectfont\color{inkTwo} #2}%
  \end{minipage}\par\vspace{0.6mm}}

%% ---- Callout / note box -----------------------------------------------
\newenvironment{callout}[1][NOTE]{%
  \begin{tikzpicture}[overlay,remember picture]\end{tikzpicture}%
  \par\vspace{2mm}\noindent
  \fcolorbox{ruleTwo}{accentSoft}{%
    \begin{minipage}{\dimexpr\linewidth-2\fboxsep-2\fboxrule}%
      {\sffamily\color{accent}\fontsize{7pt}{9pt}\selectfont\MakeUppercase{\textls[80]{#1}}}\par\vspace{1mm}
      \ignorespaces
}{%
    \end{minipage}%
  }\par\vspace{2mm}%
}

%% ---- Trace matrix table -----------------------------------------------
%% Use longtable for traceability matrices that span pages (the
%% Traceability section main.tex.j2 ends with).
%% Body setup, inside a group before each matrix longtable. Line heights
%% come from the font's ascent/descent rather than each glyph's box: in
%% tectonic's later passes XeTeX reported some 9pt words in these cells as
%% 14pt tall (a glyph box from another font), spreading their lines apart.
\newcommand{\tracetablesetup}{%
  \fontsize{9pt}{12pt}\selectfont
  \setlength{\tabcolsep}{1.5mm}%
  \setlength{\LTleft}{0pt}%  narrower than the text: keep it flush left
  \arrayrulecolor{rule}%
  \ifdefined\XeTeXuseglyphmetrics\XeTeXuseglyphmetrics=0\fi
}
\newcommand{\tracehead}[1]{%
  {\sffamily\color{inkThree}\fontsize{6.5pt}{8pt}\selectfont\MakeUppercase{\textls[60]{#1}}}}
\newcommand{\sectionrow}[1]{%
  \arrayrulecolor{rule}%
  \rowcolor{sectionRowBg}%
  \multicolumn{5}{@{}l}{%
    \sffamily\color{inkThree}\fontsize{6.5pt}{8pt}\selectfont\MakeUppercase{\textls[60]{#1}}%
  }\\}
%% Thin rule between two object rows.
\newcommand{\traceline}{\specialrule{0.2pt}{0.5mm}{0.5mm}}

%% ---- TOC styling ------------------------------------------------------
\renewcommand{\contentsname}{Table of Contents}
\titlecontents{section}[0pt]
  {\addvspace{2mm}\sffamily\color{ink}\bfseries}
  {\contentslabel{8mm}\hspace*{0pt}}
  {}
  {\titlerule*[6pt]{.}\contentspage}
\titlecontents{subsection}[12mm]
  {\sffamily\color{inkTwo}}
  {\contentslabel{12mm}}
  {}
  {\titlerule*[6pt]{.}\contentspage}
\titlecontents{subsubsection}[24mm]
  {\sffamily\color{inkThree}\small}
  {\contentslabel{14mm}}
  {}
  {\titlerule*[6pt]{.}\contentspage}

%% ---- Misc helpers -----------------------------------------------------
\newcommand{\tag}[2]{%
  \tikz[baseline=(t.base)]\node[fill=#1!12, draw=#1!40, line width=0.3pt,
    text=#1, rounded corners=0.6mm, inner xsep=2.2mm, inner ysep=0.5mm,
    font=\sffamily\fontsize{7pt}{8pt}\selectfont] (t) {\strut #2};}

\newcommand{\tagMust}{\tag{priMust}{Must}}
\newcommand{\tagShould}{\tag{priShould}{Should}}
\newcommand{\tagCould}{\tag{priCould}{Could}}
\newcommand{\tagDraft}{\tag{statusDraft}{Draft}}
\newcommand{\tagApproved}{\tag{statusApproved}{Approved}}
\newcommand{\tagImplemented}{\tag{statusImplemented}{Implemented}}

%% Default body
\setlength{\parskip}{0.4em}
\setlength{\parindent}{0pt}
\renewcommand{\baselinestretch}{1.18}
\AtBeginDocument{\color{ink}}

\endinput
