"""Standalone statistical figures; the optional plotting dependency is lazy-loaded."""


def reliability_plot(rows,path):
    import os
    from pathlib import Path
    os.environ.setdefault('MPLCONFIGDIR',str(Path(__file__).resolve().parents[2]/'.work/matplotlib'))
    import matplotlib
    matplotlib.use('Agg')
    matplotlib.rcParams['svg.hashsalt']='nhl-evaluation-1.0.0'
    import matplotlib.pyplot as plt
    seasons=sorted({r['season_id'] for r in rows})
    fig,axes=plt.subplots((len(seasons)+1)//2,2,figsize=(10,3.4*((len(seasons)+1)//2)),squeeze=False)
    for ax,season in zip(axes.flat,seasons):
        ax.plot([0,1],[0,1],color='gray',linestyle='--',linewidth=1,label='Ideal')
        for model,color in [('original','#b54a35'),('development_selected','#2376a5')]:
            points=[r for r in rows if r['season_id']==season and r['model']==model and r['n']]
            ax.plot([r['mean_probability'] for r in points],[r['observed_home_win_rate'] for r in points],
                    color=color,linewidth=1,label=model.replace('_',' '))
            ax.scatter([r['mean_probability'] for r in points],[r['observed_home_win_rate'] for r in points],
                       s=[10+0.15*r['n'] for r in points],color=color,alpha=0.75)
        ax.set(title=str(season),xlim=(0,1),ylim=(0,1),xlabel='Predicted home-win probability',ylabel='Observed home-win fraction')
        ax.grid(alpha=0.2)
        ax.legend(fontsize=8,loc='upper left')
    for ax in list(axes.flat)[len(seasons):]:ax.set_visible(False)
    fig.suptitle('Elo reliability: fixed 0.1 probability bins; marker area reflects sample count\nDescriptive calibration; repeated teams/games are not independent trials',fontsize=11)
    fig.tight_layout(rect=(0,0,1,0.94))
    fig.savefig(path,format='svg',metadata={'Date':None})
    fig.savefig(path.with_suffix('.png'),format='png',dpi=120)
    plt.close(fig)
