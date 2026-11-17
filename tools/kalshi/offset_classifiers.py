"""Offline ridge-logistic and shallow Newton boosted corrections with fixed offsets.

NumPy only. p0 is an initial logit, never an ordinary fitted tree feature.
Parameters are portable JSON dictionaries; no pickle or execution integration.
"""
from __future__ import annotations
from collections import Counter
import math
import numpy as np


class OffsetModelError(ValueError):pass


def sigmoid(z):
    z=np.asarray(z,dtype=float);p=np.empty_like(z);positive=z>=0
    p[positive]=1/(1+np.exp(-z[positive]));e=np.exp(z[~positive]);p[~positive]=e/(1+e)
    return p


def offsets(p):
    p=np.asarray(p,dtype=float)
    if not np.isfinite(p).all() or ((p<0)|(p>1)).any():raise OffsetModelError('Offset probabilities must lie in [0,1]')
    p=np.clip(p,1e-6,1-1e-6)
    return np.log(p)-np.log1p(-p)


def contract_weights(tickers):
    counts=Counter(tickers)
    if not counts:raise OffsetModelError('Training requires whole contract identities')
    return np.array([1/len(counts)/counts[t] for t in tickers],dtype=float)


def preprocessing(x,weights):
    x=np.asarray(x,dtype=float);weights=np.asarray(weights,dtype=float)
    if x.ndim!=2 or not 1<=x.shape[1]<=16 or not np.isfinite(x).all():raise OffsetModelError('Invalid classifier feature matrix')
    if weights.shape!=(len(x),) or (weights<=0).any():raise OffsetModelError('Invalid classifier weights')
    weights=weights/weights.sum();means=np.sum(x*weights[:,None],axis=0)
    scales=np.sqrt(np.sum((x-means)**2*weights[:,None],axis=0));scales=np.where(scales>1e-12,scales,1.)
    return dict(means=means.tolist(),scales=scales.tolist(),clip=8.,fit_scope='classifier_train_only')


def transform(x,preprocessor):
    x=np.asarray(x,dtype=float);means=np.array(preprocessor['means']);scales=np.array(preprocessor['scales'])
    if x.ndim!=2 or x.shape[1]!=len(means) or not np.isfinite(x).all() or not (scales>0).all():
        raise OffsetModelError('Classifier input or preprocessing dimensions differ')
    return np.clip((x-means)/scales,-8.,8.)


def ridge_objective(beta,x,initial,y,weights,ridge,*,derivatives=True):
    z=initial+x@beta
    loss=float(np.sum(weights*(np.logaddexp(0,z)-y*z))+.5*ridge*np.dot(beta,beta))
    if not derivatives:return loss
    p=sigmoid(z);g=x.T@(weights*(p-y))+ridge*beta
    h=x.T@((weights*p*(1-p))[:,None]*x)+ridge*np.eye(x.shape[1])
    return loss,g,h


def fit_ridge(x,p0,y,tickers,ridge,*,max_iterations=100):
    x=np.asarray(x,dtype=float);y=np.asarray(y,dtype=float)
    if set(y)!={0.,1.}:raise OffsetModelError('Classifier training needs both official outcome classes')
    weights=contract_weights(tickers);pre=preprocessing(x,weights)
    design=np.column_stack((np.ones(len(x)),transform(x,pre)))
    beta=np.zeros(design.shape[1]);initial=offsets(p0)
    model=dict(kind='offset_ridge_logistic',version=1,preprocessing=pre,coefficients=beta.tolist(),
        ridge=ridge,offset_coefficient=1.,offset_logit_clip=1e-6,identity=ridge is None,
        execution_eligible=False,research_only=True)
    if ridge is None:model['fit']=dict(converged=True,iterations=0);return model
    if not math.isfinite(ridge) or not 1e-6<=ridge<=1e6:raise OffsetModelError('Invalid offset ridge penalty')
    converged=False
    for iteration in range(max_iterations):
        old,g,h=ridge_objective(beta,design,initial,y,weights,ridge)
        if np.max(np.abs(g))<1e-9:converged=True;break
        step=np.linalg.solve(h,g);scale=1.
        for _ in range(40):
            candidate=beta-scale*step
            loss=ridge_objective(candidate,design,initial,y,weights,ridge,derivatives=False)
            if loss<=old-1e-4*scale*float(np.dot(g,step)):break
            scale*=.5
        else:break
        beta=candidate
    model.update(coefficients=beta.tolist(),fit=dict(converged=converged,iterations=iteration+1,
        objective=ridge_objective(beta,design,initial,y,weights,ridge,derivatives=False),
        weighted_gradient_inf=float(np.max(np.abs(ridge_objective(beta,design,initial,y,weights,ridge)[1])))))
    return model


def _tree_values(tree,x):
    if 'value' in tree:return np.full(len(x),tree['value'])
    return np.where(x[:,tree['feature']]<=tree['threshold'],_tree_values(tree['left'],x),_tree_values(tree['right'],x))


def fit_boosted(x,p0,y,tickers,*,depth=2,trees=75,learning_rate=.05,min_leaf_rows=140,leaf_ridge=.001):
    if depth not in (1,2) or trees not in (0,25,75):raise OffsetModelError('Only predeclared shallow tree configurations are supported')
    x=np.asarray(x,dtype=float);y=np.asarray(y,dtype=float);weights=contract_weights(tickers)
    pre=preprocessing(x,weights);a=transform(x,pre)
    if set(y)!={0.,1.}:raise OffsetModelError('Boosting requires both official outcome classes')
    model=dict(kind='offset_newton_boosted_trees',version=1,preprocessing=pre,depth=depth,
        trees=[],requested_trees=trees,learning_rate=learning_rate,min_leaf_rows=min_leaf_rows,
        minimum_leaf_contracts=20,leaf_ridge=leaf_ridge,max_leaf_update=2.,offset_coefficient=1.,
        offset_logit_clip=1e-6,identity=trees==0,execution_eligible=False,research_only=True)
    thresholds=[];codes=[]
    for j in range(a.shape[1]):
        cuts=np.unique(np.quantile(a[:,j],np.linspace(0,1,33)[1:-1]))
        cuts=cuts[(cuts>a[:,j].min())&(cuts<a[:,j].max())]
        thresholds.append(cuts);codes.append(np.searchsorted(cuts,a[:,j],side='left'))
    score=offsets(p0).copy();tickers=np.array(tickers)
    for _ in range(trees):
        probability=sigmoid(score);gradient=weights*(y-probability);curvature=weights*probability*(1-probability)
        def node(mask,level):
            g=float(gradient[mask].sum());h=float(curvature[mask].sum())
            value=max(-2.,min(2.,g/(h+leaf_ridge)))
            if len(set(tickers[mask]))<20:value=0.
            leaf=dict(value=value,count_rows=int(mask.sum()),count_contracts=len(set(tickers[mask])))
            if level==depth or mask.sum()<2*min_leaf_rows:return leaf
            best_gain=1e-12;best=None
            for j,cuts in enumerate(thresholds):
                if not len(cuts):continue
                bins=codes[j][mask];length=len(cuts)+1
                gs=np.cumsum(np.bincount(bins,weights=gradient[mask],minlength=length))[:-1]
                hs=np.cumsum(np.bincount(bins,weights=curvature[mask],minlength=length))[:-1]
                ns=np.cumsum(np.bincount(bins,minlength=length))[:-1]
                gains=gs*gs/(hs+leaf_ridge)+(g-gs)**2/(h-hs+leaf_ridge)-g*g/(h+leaf_ridge)
                gains=np.where((ns>=min_leaf_rows)&(mask.sum()-ns>=min_leaf_rows),gains,-np.inf)
                k=int(np.argmax(gains))
                if gains[k]>best_gain:best_gain=float(gains[k]);best=(j,float(cuts[k]))
            if best is None:return leaf
            j,cut=best;left=mask&(a[:,j]<=cut);right=mask&(a[:,j]>cut)
            return dict(feature=j,threshold=cut,gain=best_gain,left=node(left,level+1),right=node(right,level+1))
        tree=node(np.ones(len(a),dtype=bool),0)
        model['trees'].append(tree);score+=learning_rate*_tree_values(tree,a)
    model['fit']=dict(converged=True,iterations=trees,thresholds_fit_on='classifier_train_only',
        final_weighted_log_loss=float(np.sum(weights*(np.logaddexp(0,score)-y*score))))
    return model


def predict(model,x,p0):
    p0=np.asarray(p0,dtype=float)
    if model['offset_coefficient']!=1.:raise OffsetModelError('The baseline offset coefficient must remain fixed at one')
    if model['identity']:return p0.copy()
    a=transform(x,model['preprocessing']);initial=offsets(p0)
    if model['kind']=='offset_ridge_logistic':
        design=np.column_stack((np.ones(len(a)),a));score=initial+design@np.array(model['coefficients'])
    elif model['kind']=='offset_newton_boosted_trees':
        score=initial.copy()
        for tree in model['trees']:score+=model['learning_rate']*_tree_values(tree,a)
    else:raise OffsetModelError('Unsupported offline classifier kind')
    return sigmoid(score)
